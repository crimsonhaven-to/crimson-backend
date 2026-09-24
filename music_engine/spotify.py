"""Reading a user's playlists from the Spotify Web API.

Each account brings its own Spotify app (a client ID, no secret) and connects it
with Authorization Code and PKCE. The browser holds the verifier and hands the
code here once; from then on the backend refreshes and reads on its own.

Two Spotify migrations shape this, and an app registered today is past both:

| Date | What it took away |
| --- | --- |
| November 2024 | Playlists Spotify generates (Discover Weekly, Daily Mix) answer 403 |
| March 2026 | ``/playlists/{id}/tracks`` became ``/items``, each entry's ``track`` became ``item`` |
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import httpx

from core.clock import utc_now
from core.http_client import http_client

from .db import store
from .provider import ImportedPlaylist, ImportedTrack

logger = logging.getLogger("crimson.music.spotify")

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Read only. Nothing here ever writes to a Spotify account.
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
LIKED_ID = "liked"

# Refresh this long before expiry, so a request in flight cannot age out.
_EXPIRY_MARGIN = timedelta(seconds=60)
_MAX_RETRIES = 4
# Beyond this a Retry-After is reported rather than waited out.
_MAX_RETRY_AFTER = 60.0

_TRACK_FIELDS = (
    "id,name,duration_ms,type,is_local,track_number,disc_number,external_ids(isrc),"
    "artists(name),album(name,release_date,images,artists(name))"
)
_ITEM_FIELDS = f"items(added_at,is_local,item({_TRACK_FIELDS}),track({_TRACK_FIELDS})),next,total"


class SpotifyError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class SpotifyAuthError(SpotifyError):
    """The link is dead and only connecting again can fix it."""


def _expires_at(expires_in: int) -> str:
    return (utc_now() + timedelta(seconds=int(expires_in))).isoformat()


def _token_error(response: httpx.Response) -> SpotifyAuthError:
    return SpotifyAuthError(
        f"Spotify rejected the token request ({response.status_code}). Check the client ID "
        f"and the redirect URI in your Spotify app, then connect again. {response.text[:200]}",
        response.status_code,
    )


async def exchange_code(client_id: str, code: str, verifier: str, redirect_uri: str) -> dict:
    async with http_client() as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
    if response.status_code != 200:
        raise _token_error(response)
    tokens = response.json()
    if not tokens.get("refresh_token"):
        raise SpotifyAuthError("Spotify returned no refresh token, so the link cannot last.")
    return tokens


async def connect(
    user_id: int, client_id: str, code: str, verifier: str, redirect_uri: str
) -> str:
    """Trade the code for tokens, check they work by asking who they belong to,
    then store the link. Returns the Spotify display name."""
    tokens = await exchange_code(client_id, code, verifier, redirect_uri)
    async with http_client() as client:
        response = await client.get(
            f"{API_BASE}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
    if response.status_code == 403:
        # Development-mode apps answer 403 for anyone not on the app's user list.
        raise SpotifyError(
            "Spotify refused this account. Add it under User Management in your Spotify "
            "app's dashboard, or use an app you own.",
            403,
        )
    if response.status_code != 200:
        raise SpotifyError(f"Spotify refused the account lookup ({response.status_code}).")
    me = response.json()
    await asyncio.to_thread(
        store.save_link,
        user_id,
        client_id=client_id,
        refresh_token=tokens["refresh_token"],
        access_token=tokens["access_token"],
        access_expires_at=_expires_at(tokens.get("expires_in", 3600)),
        scope=tokens.get("scope") or "",
        spotify_user_id=me.get("id"),
        display_name=me.get("display_name"),
    )
    return me.get("display_name") or me.get("id") or "Spotify"


def _refresh_tokens(link: dict) -> dict:
    """Runs inside the row lock in ``store.refresh_link``, on a worker thread,
    hence the blocking client. Spotify leaves out ``refresh_token`` when it did
    not rotate, and then the stored one stays valid."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": link["refresh_token"],
            "client_id": link["client_id"],
        },
        timeout=30.0,
    )
    if response.status_code != 200:
        raise _token_error(response)
    tokens = response.json()
    return {
        "access_token": tokens["access_token"],
        "access_expires_at": _expires_at(tokens.get("expires_in", 3600)),
        "refresh_token": tokens.get("refresh_token") or link["refresh_token"],
    }


def _token_still_valid(link: dict) -> bool:
    expires = link.get("access_expires_at")
    if not link.get("access_token") or not expires:
        return False
    return datetime.fromisoformat(expires) - _EXPIRY_MARGIN > datetime.now(timezone.utc)


def _backoff(attempt: int) -> float:
    base = min(2**attempt, 16)
    return base / 2 + random.random() * base / 2


def _largest_image(images: Optional[list]) -> Optional[str]:
    if not images:
        return None
    best = max(images, key=lambda image: image.get("width") or 0)
    return best.get("url")


def parse_track(raw: Optional[dict], added_at: Optional[str] = None) -> Optional[ImportedTrack]:
    """None for what a playlist can hold that is not a song on Spotify: local
    files, podcast episodes and entries Spotify has already blanked out."""
    if not raw or raw.get("is_local") or raw.get("type", "track") != "track":
        return None
    title = raw.get("name") or ""
    artists = [a["name"] for a in raw.get("artists") or [] if a.get("name")]
    if not title or not artists:
        return None
    album = raw.get("album") or {}
    album_artists = [a["name"] for a in album.get("artists") or [] if a.get("name")]
    return ImportedTrack(
        title=title,
        artists=artists,
        spotify_id=raw.get("id"),
        isrc=(raw.get("external_ids") or {}).get("isrc"),
        album=album.get("name") or "",
        album_artist=album_artists[0] if album_artists else artists[0],
        track_number=raw.get("track_number"),
        disc_number=raw.get("disc_number"),
        release_date=album.get("release_date"),
        duration_ms=int(raw.get("duration_ms") or 0),
        cover_url=_largest_image(album.get("images")),
        added_at=added_at,
    )


def parse_playlist_entry(entry: dict) -> Optional[ImportedTrack]:
    """Reads both names for the wrapped track, because Spotify still serves the
    pre-March 2026 ``track`` alongside ``item``."""
    if entry.get("is_local"):
        return None
    return parse_track(entry.get("item") or entry.get("track"), entry.get("added_at"))


class SpotifyClient:
    """One account's view of Spotify."""

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def _token(self, force: bool = False) -> str:
        def _check(link: dict) -> bool:
            return not force and _token_still_valid(link)

        try:
            link = await asyncio.to_thread(store.refresh_link, self.user_id, _check, _refresh_tokens)
        except httpx.HTTPError as e:
            raise SpotifyError(f"Could not reach Spotify: {e}") from e
        if link is None:
            raise SpotifyAuthError("No Spotify account is connected.")
        return link["access_token"]

    async def get(self, path_or_url: str, params: Optional[dict] = None) -> Any:
        """One request, with the three failures Spotify actually produces handled:
        an expired token, a rate limit and a transient server error."""
        url = path_or_url if path_or_url.startswith("http") else f"{API_BASE}{path_or_url}"
        refreshed = False
        attempt = 0
        async with http_client() as client:
            while True:
                token = await self._token(force=refreshed)
                try:
                    response = await client.get(
                        url, params=params, headers={"Authorization": f"Bearer {token}"}
                    )
                except httpx.HTTPError as e:
                    if attempt >= _MAX_RETRIES:
                        raise SpotifyError(f"Could not reach Spotify: {e}") from e
                    await asyncio.sleep(_backoff(attempt))
                    attempt += 1
                    continue

                if response.status_code == 200:
                    return response.json()
                # Exactly one forced refresh: a second 401 means the token is not it.
                if response.status_code == 401 and not refreshed:
                    refreshed = True
                    continue
                if response.status_code == 401:
                    raise SpotifyAuthError("Spotify rejected the session. Connect again.", 401)
                if response.status_code == 429:
                    wait = float(response.headers.get("Retry-After") or 1)
                    if wait > _MAX_RETRY_AFTER or attempt >= _MAX_RETRIES:
                        raise SpotifyError(
                            f"Spotify is rate limiting this app. Try again in {int(wait)}s.", 429
                        )
                    await asyncio.sleep(wait)
                    attempt += 1
                    continue
                if response.status_code == 403:
                    raise SpotifyError(
                        "Spotify will not hand over that playlist. The ones it builds for you, "
                        "like Discover Weekly and Daily Mix, are closed to apps registered after "
                        "November 2024. Playlists people made are not affected.",
                        403,
                    )
                if response.status_code >= 500 and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_backoff(attempt))
                    attempt += 1
                    continue
                raise SpotifyError(
                    f"Spotify refused the request ({response.status_code}). "
                    f"{response.text[:200]}",
                    response.status_code,
                )

    async def _pages(self, path: str, params: Optional[dict] = None) -> AsyncIterator[dict]:
        """Every item of a paged endpoint. Spotify hands back an absolute ``next``."""
        page = await self.get(path, params)
        while True:
            for item in page.get("items") or []:
                yield item
            if not page.get("next"):
                return
            page = await self.get(page["next"])

    async def my_playlists(self) -> list[dict]:
        """``/me/playlists`` takes no ``fields`` mask. The track count moved from
        ``tracks.total`` to ``items.total``, and either may be what comes back."""
        out = []
        async for playlist in self._pages("/me/playlists", {"limit": 50}):
            if not playlist:
                continue
            counts = playlist.get("items") or playlist.get("tracks") or {}
            out.append(
                {
                    "spotify_id": playlist["id"],
                    "name": playlist.get("name") or "",
                    "owner": (playlist.get("owner") or {}).get("display_name") or "",
                    "cover_url": _largest_image(playlist.get("images")),
                    "track_count": counts.get("total") if isinstance(counts, dict) else None,
                }
            )
        return out

    async def playlist(self, playlist_id: str) -> ImportedPlaylist:
        if playlist_id == LIKED_ID:
            return await self.liked_songs()
        meta = await self.get(
            f"/playlists/{playlist_id}",
            {"fields": "id,name,description,snapshot_id,images"},
        )
        tracks = []
        async for entry in self._pages(
            f"/playlists/{playlist_id}/items", {"fields": _ITEM_FIELDS, "limit": 100}
        ):
            track = parse_playlist_entry(entry)
            if track:
                tracks.append(track)
        return ImportedPlaylist(
            name=meta.get("name") or "Untitled playlist",
            description=meta.get("description") or "",
            cover_url=_largest_image(meta.get("images")),
            snapshot_id=meta.get("snapshot_id"),
            tracks=tracks,
        )

    async def snapshot_id(self, playlist_id: str) -> Optional[str]:
        """Cheap change detection. Liked Songs has none, so it always resyncs."""
        if playlist_id == LIKED_ID:
            return None
        meta = await self.get(f"/playlists/{playlist_id}", {"fields": "snapshot_id"})
        return meta.get("snapshot_id")

    async def liked_songs(self) -> ImportedPlaylist:
        tracks = []
        async for entry in self._pages("/me/tracks", {"limit": 50}):
            track = parse_track(entry.get("track"), entry.get("added_at"))
            if track:
                tracks.append(track)
        return ImportedPlaylist(name="Liked Songs", tracks=tracks)
