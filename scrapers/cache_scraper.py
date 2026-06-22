"""
Cache scraper — surfaces already-cached episodes as a first-class source.

When the server-side video cache holds a *ready* file for the requested episode
(downloaded on a previous play — see ``cache_engine.downloader``), this emits a
``crimson-cache:{token}`` embed per cached language. The ``CacheResolver`` turns
each into a direct-play ``/cache_proxy`` stream labelled with the NAS target's
admin-given name.

It runs whenever at least one cache target is enabled (independent of the global
download switch — even with new downloads turned off, existing cache still
plays). It does no scraping/network: it's a single indexed DB lookup keyed on the
TMDB id + season + episode the watch pipeline already has.
"""

from __future__ import annotations

import asyncio
import os
from typing import List, Optional

from cache_engine.db import CacheStore
from cache_engine.fs import EMBED_MARKER, encode_token, is_configured

from .base_scraper import BaseAnimeScraper

_store = CacheStore()


class CacheScraper(BaseAnimeScraper):
    """Locates ready cache entries for an episode by TMDB id + season + episode."""

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        # No network — just stash the TMDB id for get_episode_embeds. Returning a
        # truthy slug keeps the pipeline going; None short-circuits it.
        if not is_configured():
            return None
        tmdb_id = media_ctx.get("tmdb_id")
        if not tmdb_id:
            return None
        self._tmdb_id = int(tmdb_id)
        return str(tmdb_id)

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> List[dict]:
        tmdb_id = getattr(self, "_tmdb_id", None)
        if not tmdb_id or not is_configured():
            return []

        rows = await asyncio.to_thread(
            _store.ready_for_episode, tmdb_id, season_num, episode_num
        )
        embeds: List[dict] = []
        for row in rows:
            abs_path = os.path.join(row["target_path"], row["rel_path"])
            embeds.append({
                "url": f"{EMBED_MARKER}:{encode_token(abs_path)}",
                "language": row.get("language") or None,
            })
        if embeds:
            print(f"[CacheScraper] {len(embeds)} cached file(s) for tmdb-{tmdb_id} S{season_num}E{episode_num}")
        return embeds
