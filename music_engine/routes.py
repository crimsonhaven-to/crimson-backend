"""The music library for a granted account: the Spotify link, playlists, and the
tracks that need a person to pick a recording."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.public_url import public_base_url
from core.rate_limit import limiter

from . import cdn, covers, fs, library, spotify
from .access import require_music_user
from .csv_import import CsvImportError
from .db import store
from .payloads import playlist_payload, track_payload
from .provider import ProviderError, get_provider
from .schemas import (
    CsvImport,
    LocalPlaylist,
    MatchChoice,
    PlaylistClone,
    PlaylistImport,
    PlaylistUpdate,
    SongAdd,
    SpotifyConnect,
)
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


_USER_ERRORS = (
    SpotifyError, ProviderError, library.LibraryError, CsvImportError, covers.CoverError
)


def _base(request: Request) -> str:
    return public_base_url(request).rstrip("/")


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
        "cdn": cdn.enabled(),
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
async def list_playlists(request: Request, user: dict = Depends(require_music_user)):
    rows = await asyncio.to_thread(store.list_playlists, user["user_id"])
    base = _base(request)
    return {"success": True, "playlists": [playlist_payload(row, base) for row in rows]}


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


@router.post("/playlists/local")
@limiter.limit("20/minute")
async def create_local_playlist(
    request: Request, body: LocalPlaylist, user: dict = Depends(require_music_user)
):
    return {"success": True, **await library.create_local(user["user_id"], body.name.strip())}


@router.get("/all-titles")
async def all_titles(request: Request, user: dict = Depends(require_music_user)):
    rows = await asyncio.to_thread(store.all_titles, user["user_id"])
    base = _base(request)
    tracks = [
        {**track_payload(row, base), "playlists": row.get("playlists") or []}
        for row in rows
    ]
    return {"success": True, "total": len(tracks), "tracks": tracks}


@router.get("/playlists/{playlist_id}")
async def get_playlist(
    request: Request, playlist_id: int, user: dict = Depends(require_music_user)
):
    playlist = await _owned_playlist(user, playlist_id)
    rows = await asyncio.to_thread(store.playlist_tracks, playlist_id)
    base = _base(request)
    return {
        "success": True,
        "playlist": playlist_payload(playlist, base),
        "tracks": [track_payload(row, base) for row in rows],
    }


@router.patch("/playlists/{playlist_id}")
async def update_playlist(
    request: Request,
    playlist_id: int,
    body: PlaylistUpdate,
    user: dict = Depends(require_music_user),
):
    await _owned_playlist(user, playlist_id)
    await asyncio.to_thread(
        store.update_playlist, user["user_id"], playlist_id, sync_enabled=body.sync_enabled
    )
    playlist = await _owned_playlist(user, playlist_id)
    return {"success": True, "playlist": playlist_payload(playlist, _base(request))}


@router.post("/playlists/{playlist_id}/clone")
@limiter.limit("20/minute")
async def clone_playlist(
    request: Request, playlist_id: int, body: PlaylistClone, user: dict = Depends(require_music_user)
):
    """Copies an imported playlist into one the member can edit. The songs are
    shared, so nothing is downloaded twice."""
    playlist = await library.clone(user["user_id"], playlist_id, body.name.strip())
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"success": True, "playlist": playlist_payload(playlist, _base(request))}


async def _read_capped(request: Request) -> bytes:
    """Stops reading past the cap, so an oversized upload never sits in memory whole."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > covers.MAX_BYTES:
            break
    return bytes(body)


@router.put("/playlists/{playlist_id}/cover")
@limiter.limit("20/minute")
async def set_playlist_cover(
    request: Request, playlist_id: int, user: dict = Depends(require_music_user)
):
    """The body is the picture itself."""
    await _owned_playlist(user, playlist_id)
    data = await _read_capped(request)
    try:
        kind = covers.content_type(data)
    except covers.CoverError as e:
        raise _error(e)
    await asyncio.to_thread(store.set_cover, playlist_id, kind, data)
    playlist = await _owned_playlist(user, playlist_id)
    return {"success": True, "playlist": playlist_payload(playlist, _base(request))}


@router.delete("/playlists/{playlist_id}/cover")
async def delete_playlist_cover(
    request: Request, playlist_id: int, user: dict = Depends(require_music_user)
):
    """Back to the cover the import brought, if any."""
    await _owned_playlist(user, playlist_id)
    await asyncio.to_thread(store.delete_cover, playlist_id)
    playlist = await _owned_playlist(user, playlist_id)
    return {"success": True, "playlist": playlist_payload(playlist, _base(request))}


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
    if not await library.delete(user["user_id"], playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"success": True}


@router.post("/playlists/{playlist_id}/tracks")
@limiter.limit("60/minute")
async def add_song(
    request: Request, playlist_id: int, body: SongAdd, user: dict = Depends(require_music_user)
):
    playlist = await _owned_playlist(user, playlist_id)
    song = library.song_from_search(body.title, body.channel, body.duration_ms, body.thumbnail_url)
    try:
        result = await library.add_song(playlist, body.url, song)
    except _USER_ERRORS as e:
        raise _error(e)
    return {"success": True, **result}


@router.delete("/playlists/{playlist_id}/tracks/{track_id}")
async def remove_song(playlist_id: int, track_id: int, user: dict = Depends(require_music_user)):
    """The entry only: the song stays on the share."""
    playlist = await _owned_playlist(user, playlist_id)
    try:
        removed = await library.remove_song(playlist, track_id)
    except _USER_ERRORS as e:
        raise _error(e)
    if not removed:
        raise HTTPException(status_code=404, detail="That song is not in this playlist")
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
