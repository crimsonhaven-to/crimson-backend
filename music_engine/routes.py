"""The music library for a granted account: the Spotify link, playlists, and the
tracks that need a person to pick a recording."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.public_url import public_base_url
from core.rate_limit import limiter

from . import fs, library, links, spotify
from .access import require_music_user
from .csv_import import CsvImportError
from .db import store
from .provider import ProviderError, get_provider
from .schemas import CsvImport, MatchChoice, PlaylistImport, PlaylistUpdate, SpotifyConnect
from .spotify import LIKED_ID, SCOPES, SpotifyAuthError, SpotifyClient, SpotifyError

router = APIRouter(prefix="/music", tags=["music"])


def _error(e: Exception) -> HTTPException:
    """A dead Spotify link is 409, never 401: the client reads a 401 as its own
    session ending and would log the user out."""
    if isinstance(e, SpotifyAuthError):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, (SpotifyError, ProviderError)):
        return HTTPException(status_code=502, detail=str(e))
    return HTTPException(status_code=400, detail=str(e))


_USER_ERRORS = (SpotifyError, ProviderError, library.LibraryError, CsvImportError)


def _track_payload(row: dict, base: str) -> dict:
    ready = row["status"] == "ready"
    return {
        "id": row["id"],
        "spotify_id": row["spotify_id"],
        "title": row["title"],
        "artists": row["artists"],
        "album": row["album"],
        "duration_ms": row["duration_ms"],
        "status": row["status"],
        "error": row["error"],
        "removed_upstream": bool(row.get("removed_upstream")),
        "cover_url": base + links.signed_path(links.ART, row["id"])
        if row["cover_path"]
        else row["cover_url"],
        "stream_url": base + links.signed_path(links.STREAM, row["id"]) if ready else None,
    }


async def _owned_track(user: dict, track_id: int) -> dict:
    if not await asyncio.to_thread(store.user_has_track, user["user_id"], track_id):
        raise HTTPException(status_code=404, detail="Track not found")
    track = await asyncio.to_thread(store.get_track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track


async def _owned_playlist(user: dict, playlist_id: int) -> dict:
    playlist = await asyncio.to_thread(store.get_playlist, user["user_id"], playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist


# --- status and the Spotify link ------------------------------------------------------
@router.get("/status")
async def music_status(user: dict = Depends(require_music_user)):
    link, counts = await asyncio.gather(
        asyncio.to_thread(store.get_link, user["user_id"]),
        asyncio.to_thread(store.queue_counts, user["user_id"]),
    )
    return {
        "success": True,
        "provider": get_provider() is not None,
        "share_ready": await asyncio.to_thread(fs.available),
        "scopes": SCOPES,
        "spotify": {
            "connected": link is not None,
            "client_id": link["client_id"] if link else None,
            "display_name": link["display_name"] if link else None,
        },
        "counts": counts,
    }


@router.post("/spotify/connect")
@limiter.limit("10/minute")
async def connect_spotify(
    request: Request, body: SpotifyConnect, user: dict = Depends(require_music_user)
):
    try:
        name = await spotify.connect(
            user["user_id"], body.client_id, body.code, body.code_verifier, body.redirect_uri
        )
    except SpotifyError as e:
        raise _error(e)
    return {"success": True, "display_name": name}


@router.delete("/spotify")
async def disconnect_spotify(user: dict = Depends(require_music_user)):
    """Imported playlists stay. They stop syncing until a link exists again."""
    await asyncio.to_thread(store.delete_link, user["user_id"])
    return {"success": True}


@router.get("/spotify/playlists")
async def spotify_playlists(user: dict = Depends(require_music_user)):
    try:
        playlists = await SpotifyClient(user["user_id"]).my_playlists()
    except SpotifyError as e:
        raise _error(e)
    imported = {
        p["spotify_id"]: p["id"]
        for p in await asyncio.to_thread(store.list_playlists, user["user_id"])
        if p["source"] == library.SPOTIFY
    }
    liked = {"spotify_id": LIKED_ID, "name": "Liked Songs", "owner": "", "cover_url": None,
             "track_count": None}
    return {
        "success": True,
        "playlists": [
            {**p, "imported_id": imported.get(p["spotify_id"])} for p in [liked, *playlists]
        ],
    }


# --- playlists ------------------------------------------------------------------------
@router.get("/playlists")
async def list_playlists(user: dict = Depends(require_music_user)):
    return {
        "success": True,
        "playlists": await asyncio.to_thread(store.list_playlists, user["user_id"]),
    }


@router.post("/playlists")
@limiter.limit("20/minute")
async def import_playlist(
    request: Request, body: PlaylistImport, user: dict = Depends(require_music_user)
):
    try:
        result = await library.import_spotify(user["user_id"], body.source, body.playlist)
    except _USER_ERRORS as e:
        raise _error(e)
    return {"success": True, **result}


@router.post("/playlists/csv")
@limiter.limit("20/minute")
async def import_csv(request: Request, body: CsvImport, user: dict = Depends(require_music_user)):
    try:
        result = await library.import_csv(user["user_id"], body.name.strip(), body.csv)
    except _USER_ERRORS as e:
        raise _error(e)
    return {"success": True, **result}


@router.get("/playlists/{playlist_id}")
async def get_playlist(
    request: Request, playlist_id: int, user: dict = Depends(require_music_user)
):
    playlist = await _owned_playlist(user, playlist_id)
    rows = await asyncio.to_thread(store.playlist_tracks, playlist_id)
    base = public_base_url(request).rstrip("/")
    return {
        "success": True,
        "playlist": playlist,
        "tracks": [_track_payload(row, base) for row in rows],
    }


@router.patch("/playlists/{playlist_id}")
async def update_playlist(
    playlist_id: int, body: PlaylistUpdate, user: dict = Depends(require_music_user)
):
    await _owned_playlist(user, playlist_id)
    playlist = await asyncio.to_thread(
        store.update_playlist, user["user_id"], playlist_id, sync_enabled=body.sync_enabled
    )
    return {"success": True, "playlist": playlist}


@router.post("/playlists/{playlist_id}/sync")
@limiter.limit("20/minute")
async def sync_playlist(request: Request, playlist_id: int, user: dict = Depends(require_music_user)):
    playlist = await _owned_playlist(user, playlist_id)
    try:
        result = await library.sync({**playlist, "snapshot_id": None})
    except _USER_ERRORS as e:
        raise _error(e)
    return {"success": True, **result}


@router.delete("/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int, user: dict = Depends(require_music_user)):
    """The playlist goes; its tracks and files stay in the library."""
    if not await asyncio.to_thread(store.delete_playlist, user["user_id"], playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"success": True}


# --- tracks ---------------------------------------------------------------------------
@router.get("/tracks/{track_id}/candidates")
async def track_candidates(track_id: int, user: dict = Depends(require_music_user)):
    track = await _owned_track(user, track_id)
    return {"success": True, "candidates": track.get("candidates") or []}


@router.post("/tracks/{track_id}/match")
async def choose_match(
    track_id: int, body: MatchChoice, user: dict = Depends(require_music_user)
):
    await _owned_track(user, track_id)
    await asyncio.to_thread(store.choose_match, track_id, body.url)
    return {"success": True}


@router.post("/tracks/{track_id}/retry")
async def retry_track(track_id: int, user: dict = Depends(require_music_user)):
    await _owned_track(user, track_id)
    await asyncio.to_thread(store.retry, track_id)
    return {"success": True}


@router.get("/search")
@limiter.limit("30/minute")
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200),
    user: dict = Depends(require_music_user),
):
    provider = get_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail="This server has no music provider.")
    try:
        results = await provider.search(q)
    except ProviderError as e:
        raise _error(e)
    return {"success": True, "results": [vars(c) for c in results]}
