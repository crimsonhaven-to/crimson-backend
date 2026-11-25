"""Genres for the library, from what imports carry and from Spotify.

| Source | When | Covers |
| --- | --- | --- |
| Spotify artist ids on a track | import and sync | every Spotify import |
| An Exportify CSV's genre column | CSV import | that file's tracks |
| Spotify search by artist name | worker, once per track | songs added by search, CSVs without ids |

Spotify only knows genres per artist, so the worker looks each artist up once,
keeps the answer in music_artists for every member, and paces itself, since a
development-mode app has one request per artist and a tight rate limit.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable

from . import genres_db
from .spotify import SpotifyClient, SpotifyError

logger = logging.getLogger("crimson.music.genres")

_SEPARATORS = re.compile(r"[,;]")
_SPACES = re.compile(r"\s+")
# Spotify answers these for a bad id; anything else may work on a later try.
_GONE = (400, 404)


def normalize(values: Iterable[str]) -> list[str]:
    """Lowercase, single spaced and deduplicated, in the order given. A value
    may hold several genres separated by commas or semicolons, as CSV cells do."""
    out: list[str] = []
    for value in values:
        for part in _SEPARATORS.split(value or ""):
            genre = _SPACES.sub(" ", part).strip().lower()
            if genre and genre not in out:
                out.append(genre)
    return out


async def enrich(budget: int) -> int:
    """Spend at most ``budget`` Spotify requests on genres. Returns how many
    were made. A failure stops the pass and propagates, so the caller can back off."""
    user_id = await asyncio.to_thread(genres_db.lookup_user)
    if user_id is None or budget <= 0:
        return 0
    client = SpotifyClient(user_id)
    used = 0

    for spotify_id in await asyncio.to_thread(genres_db.unknown_artist_ids, budget):
        used += 1
        try:
            artist = await client.artist(spotify_id)
        except SpotifyError as e:
            if e.status not in _GONE:
                raise
            artist = {}
        await asyncio.to_thread(
            genres_db.save_artist,
            spotify_id,
            artist.get("name") or "",
            normalize(artist.get("genres") or []),
        )

    for track in await asyncio.to_thread(genres_db.tracks_without_artist_ids, budget - used):
        used += 1
        found = await client.find_artist(track["artists"][0])
        # The artist's genres come on a later pass, from the artist itself.
        await asyncio.to_thread(
            genres_db.set_artist_ids, track["id"], [found["id"]] if found else []
        )
    return used
