"""Surfaces episodes the server-side cache already holds, one embed per language.

Runs whenever a cache target is enabled, even with new downloads switched off,
so existing cache keeps playing. No network: one indexed lookup on the TMDB id,
season and episode the pipeline already has.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

from cache_engine.db import store
from cache_engine.fs import EMBED_MARKER, encode_token, is_configured

from .base_scraper import BaseAnimeScraper

logger = logging.getLogger(__name__)


class CacheScraper(BaseAnimeScraper):
    SUPPORTS_MOVIES = True

    _tmdb_id: Optional[int] = None
    _media_type: str = "tv"

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        if not is_configured():
            return None
        tmdb_id = media_ctx.get("tmdb_id")
        if not tmdb_id:
            return None
        self._tmdb_id = int(tmdb_id)
        self._media_type = media_ctx.get("media_type") or "tv"
        return str(tmdb_id)

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> List[dict]:
        tmdb_id = self._tmdb_id
        if not tmdb_id or not is_configured():
            return []

        # The cache stores movies at season 0 episode 0, as the watch path and
        # mint_ticket do.
        if self._media_type == "movie":
            season_num, episode_num = 0, 0

        rows = await asyncio.to_thread(
            store.ready_for_episode, tmdb_id, season_num, episode_num, self._media_type
        )
        embeds = [
            {
                "url": f"{EMBED_MARKER}:{encode_token(os.path.join(row['target_path'], row['rel_path']))}",
                "language": row.get("language") or None,
            }
            for row in rows
        ]
        if embeds:
            logger.info(
                "Cache: %d cached file(s) for %s tmdb-%s S%sE%s",
                len(embeds), self._media_type, tmdb_id, season_num, episode_num,
            )
        return embeds
