"""Importing and syncing playlists into the library.

Four ways in, one way to store:

| Source | Reads | Syncs | Needs |
| --- | --- | --- | --- |
| ``spotify`` | the Web API, private playlists and Liked Songs | yes | a connected Spotify app (Premium) |
| ``public`` | the public embed, first 100 tracks, no album | yes | the music provider |
| ``csv`` | an Exportify file | no | nothing |
| ``local`` | nothing: the member adds songs found by search | no | the music provider |
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import timedelta
from typing import Optional

from account_engine.db import store as account_store
from core.clock import utc_now

from . import fs
from .csv_import import parse_csv
from .db import store
from .provider import ImportedPlaylist, ImportedTrack, get_provider
from .spotify import LIKED_ID, SpotifyClient

logger = logging.getLogger("crimson.music.library")

SPOTIFY = "spotify"
PUBLIC = "public"
CSV = "csv"
LOCAL = "local"

SYNC_INTERVAL = timedelta(hours=6)

_PLAYLIST_ID = re.compile(r"(?:playlist[/:])?([A-Za-z0-9]{22})\b")
# The noise uploaders put after a song's name. Only bracketed, so a title that
# merely contains "live" or "audio" keeps it.
_TITLE_NOISE = re.compile(
    r"\s*[(\[](?:official[^)\]]*|lyrics?|lyric video|audio|visuali[sz]er|hd|hq|4k|mv)[)\]]",
    re.IGNORECASE,
)


class LibraryError(Exception):
    """A reason the user can act on."""


def parse_playlist_id(value: str) -> Optional[str]:
    """The id in a share link, a ``spotify:playlist:`` URI or a bare id."""
    value = (value or "").strip()
    if value == LIKED_ID:
        return value
    match = _PLAYLIST_ID.search(value)
    return match.group(1) if match else None


def track_key(track: ImportedTrack) -> str:
    """The Spotify id when there is one. Otherwise the ISRC, and failing that a
    hash of what a person would recognise the song by."""
    if track.spotify_id:
        return track.spotify_id
    if track.isrc:
        return f"isrc:{track.isrc.upper()}"
    seconds = round(track.duration_ms / 1000)
    raw = f"{track.artists[0].lower()}|{track.title.lower()}|{seconds}"
    return "meta:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def song_from_search(title: str, channel: str, duration_ms: int, cover_url: str) -> ImportedTrack:
    """A best guess at a search result's song. YouTube Music's "Artist - Topic"
    channels name the artist; elsewhere uploads are usually "Artist - Title".
    The worker replaces the guess with the source's own metadata when the
    download carries any."""
    clean = _TITLE_NOISE.sub("", title).strip() or title.strip()
    artist = channel.removesuffix(" - Topic").strip()
    if not channel.endswith(" - Topic") and " - " in clean:
        artist, clean = (part.strip() for part in clean.split(" - ", 1))
    return ImportedTrack(
        title=clean,
        artists=[artist] if artist else [],
        duration_ms=duration_ms,
        cover_url=cover_url or None,
    )


def search_key(url: str) -> str:
    return "url:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


async def _read(source: str, user_id: int, spotify_id: str) -> ImportedPlaylist:
    if source == SPOTIFY:
        return await SpotifyClient(user_id).playlist(spotify_id)
    provider = get_provider()
    if provider is None:
        raise LibraryError("This server has no music provider, so public links cannot be read.")
    return await provider.public_playlist(spotify_id)


async def _store(user_id: int, source: str, spotify_id: Optional[str], playlist: ImportedPlaylist,
                 sync_enabled: bool) -> dict:
    row = await asyncio.to_thread(
        store.upsert_playlist,
        user_id,
        source=source,
        spotify_id=spotify_id,
        name=playlist.name,
        description=playlist.description,
        cover_url=playlist.cover_url,
        sync_enabled=sync_enabled,
    )
    entries = [(track_key(track), track) for track in playlist.tracks]
    summary = await asyncio.to_thread(store.save_tracks, row["id"], entries)
    await asyncio.to_thread(
        store.mark_synced, row["id"], snapshot_id=playlist.snapshot_id, error=None
    )
    await asyncio.to_thread(write_playlist_file, row)
    return {"playlist": row, **summary}


async def import_spotify(user_id: int, source: str, value: str) -> dict:
    spotify_id = parse_playlist_id(value)
    if not spotify_id:
        raise LibraryError("That does not look like a Spotify playlist link.")
    if source == PUBLIC and spotify_id == LIKED_ID:
        raise LibraryError("Liked Songs is private, so it needs a connected Spotify account.")
    playlist = await _read(source, user_id, spotify_id)
    return await _store(user_id, source, spotify_id, playlist, sync_enabled=True)


async def import_csv(user_id: int, name: str, text: str) -> dict:
    tracks = parse_csv(text)
    playlist = ImportedPlaylist(name=name, tracks=tracks)
    return await _store(user_id, CSV, None, playlist, sync_enabled=False)


async def create_local(user_id: int, name: str) -> dict:
    playlist = await asyncio.to_thread(
        store.upsert_playlist,
        user_id,
        source=LOCAL,
        spotify_id=None,
        name=name,
        description="",
        cover_url=None,
        sync_enabled=False,
    )
    return {"playlist": playlist}


def _require_local(playlist: dict) -> None:
    if playlist["source"] != LOCAL:
        raise LibraryError("Imported playlists follow their source. Add songs to one of your own.")


async def add_song(playlist: dict, url: str, song: ImportedTrack) -> dict:
    _require_local(playlist)
    result = await asyncio.to_thread(store.add_song, playlist["id"], search_key(url), song, url)
    track = await asyncio.to_thread(store.get_track, result["track_id"])
    if track and track["status"] == "ready":
        await asyncio.to_thread(write_playlist_file, playlist)
    return result


async def remove_song(playlist: dict, track_id: int) -> bool:
    _require_local(playlist)
    removed = await asyncio.to_thread(store.remove_song, playlist["id"], track_id)
    if removed:
        await asyncio.to_thread(write_playlist_file, playlist)
    return removed


async def sync(playlist: dict) -> dict:
    """Re-read one playlist. A Spotify playlist whose snapshot has not moved is
    only stamped; everything else is read in full and diffed by save_tracks."""
    source, spotify_id = playlist["source"], playlist["spotify_id"]
    if source == CSV or not spotify_id:
        return {"skipped": True}
    try:
        if source == SPOTIFY and playlist.get("snapshot_id"):
            snapshot = await SpotifyClient(playlist["user_id"]).snapshot_id(spotify_id)
            if snapshot == playlist["snapshot_id"]:
                await asyncio.to_thread(
                    store.mark_synced, playlist["id"], snapshot_id=None, error=None
                )
                return {"unchanged": True}
        fresh = await _read(source, playlist["user_id"], spotify_id)
        return await _store(playlist["user_id"], source, spotify_id, fresh, sync_enabled=True)
    except Exception as e:
        await asyncio.to_thread(store.mark_synced, playlist["id"], snapshot_id=None, error=str(e))
        raise


async def sync_due() -> int:
    """Playlists not synced within SYNC_INTERVAL, a few per call. One broken
    playlist is logged and stamped, and does not stop the rest."""
    cutoff = (utc_now() - SYNC_INTERVAL).isoformat()
    synced = 0
    for playlist in await asyncio.to_thread(store.due_for_sync, cutoff):
        try:
            await sync(playlist)
            synced += 1
        except Exception as e:
            logger.warning("sync of playlist %s failed: %s", playlist["id"], e)
    return synced


def write_playlist_file(playlist: dict) -> None:
    """Best effort: the M3U is a convenience for other players, and a share
    hiccup must not fail an import."""
    if not fs.available():
        return
    try:
        account = account_store.get_account(playlist["user_id"]) or {}
        owner = account.get("username") or account.get("label") or f"user-{playlist['user_id']}"
        entries = store.ready_paths_for_playlist(playlist["id"])
        fs.write_playlist_file(owner, playlist["name"], entries)
    except Exception as e:
        logger.warning("could not write the playlist file for %s: %s", playlist["id"], e)
