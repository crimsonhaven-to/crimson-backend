"""Next-episode hints for Continue Watching, so the client never offers an
episode that does not exist or has not aired yet."""

import asyncio
from typing import Dict, List

from .tmdb import season_episode_info

_CONCURRENCY = 8


async def annotate(rows: List[Dict]) -> List[Dict]:
    """Add ``season_episode_count``, ``next_episode_exists`` and
    ``next_episode_air_date`` to each row, in place. Best effort: a row whose
    season cannot be fetched is left as it was."""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(row: Dict) -> None:
        tmdb_id, season = row.get("tmdb_id"), row.get("season_number")
        episode = row.get("episode_number")
        if not tmdb_id or season is None:
            return
        async with sem:
            info = await season_episode_info(int(tmdb_id), int(season))
        if not info:
            return
        row["season_episode_count"] = info.get("count")
        if episode is not None:
            air_dates = info.get("air_dates") or {}
            row["next_episode_exists"] = int(episode) + 1 in air_dates
            row["next_episode_air_date"] = air_dates.get(int(episode) + 1)

    await asyncio.gather(*(_one(r) for r in rows), return_exceptions=True)
    return rows
