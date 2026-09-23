"""Anime search, answered from the local catalogue first."""

import asyncio
from typing import Dict, List

from core.http_client import http_client

from .catalogue import search_anime_entries
from .tmdb import fetch_tmdb_search_results

# Below this many local hits TMDB is asked too, so a title added upstream since
# the last Fribb sync still resolves. Above it, TMDB has nothing to add: its
# results without a local AniList mapping are discarded anyway.
LOCAL_SEARCH_FLOOR = 3


async def search_anime(query: str, tmdb_enabled: bool) -> List[Dict]:
    """The client searches on every keystroke, so the round trip is the cost that
    matters. Local rows come first: they are ranked against the query, where
    TMDB's order reflects TMDB's own popularity."""
    results = await asyncio.to_thread(search_anime_entries, query)
    if len(results) < LOCAL_SEARCH_FLOOR and tmdb_enabled:
        async with http_client() as client:
            remote = await fetch_tmdb_search_results(client, query)
        seen = {r["anilist_id"] for r in results}
        results += [r for r in remote if r["anilist_id"] not in seen]
    return results
