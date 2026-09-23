"""The ids and fallback title a /watch call starts from."""

import asyncio
import logging
from typing import Optional, Tuple

from core.http_client import http_client
from metadata_engine import catalogue
from metadata_engine.tmdb import fetch_tmdb_movie, fetch_tmdb_show

logger = logging.getLogger("crimson.watch")


async def tv_start(tmdb_id: int, season_number: int) -> Tuple[Optional[int], Optional[str]]:
    """(anilist_id, fallback_title). A TMDB-only season of a long show has no
    AniList mapping, so its title comes from the stored row or TMDB, for the
    title-matching sources. Sources keyed on the TMDB id play either way."""
    anilist_id = await asyncio.to_thread(catalogue.get_anilist_id, tmdb_id, season_number)
    if anilist_id:
        return anilist_id, None
    title = (await asyncio.to_thread(catalogue.get_show_info, tmdb_id)).get("title")
    if not title:
        try:
            async with http_client() as client:
                title = (await fetch_tmdb_show(client, tmdb_id)).get("title")
        except Exception as e:
            logger.warning(f"show title fetch failed for {tmdb_id}: {e}")
    return None, title


async def movie_title(tmdb_id: int) -> Optional[str]:
    title = (await asyncio.to_thread(catalogue.get_movie_info, tmdb_id)).get("title")
    if title:
        return title
    try:
        async with http_client() as client:
            return (await fetch_tmdb_movie(client, tmdb_id)).get("title")
    except Exception as e:
        logger.warning(f"movie title fetch failed for {tmdb_id}: {e}")
        return None
