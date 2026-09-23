"""The windowed AniList airing-schedule fetch.

Not ``fetch_anilist_metadata``: that is one title per request and writes the
shared response cache, while ``Page.airingSchedules`` answers the whole window in
a handful of requests however many titles are followed. The window is not
restricted to followed ids either: that costs nothing extra and is what lets the
calendar show everything airing this week.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx

from core.clock import utc_now
from metadata_engine.anilist import anilist_post

logger = logging.getLogger("crimson.airing")

_PER_PAGE = 50  # AniList's ceiling

# A guard, not a target. A week of anime is a few hundred schedules, so hitting
# this means the window or the upstream is not what we think, and the poller
# should stop rather than walk pages until it is rate limited.
_MAX_PAGES = 40

_QUERY = """
query ($page: Int, $perPage: Int, $from: Int, $to: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    airingSchedules(airingAt_greater: $from, airingAt_lesser: $to, sort: TIME) {
      mediaId
      episode
      airingAt
      media { title { romaji english } }
    }
  }
}
"""


def _title(entry: dict) -> Optional[str]:
    """AniList's name for the show, English first. Carried because the other
    source, anime_entries, lags a new season behind the Fribb resync, so the
    shows most worth following would otherwise render as a bare id."""
    titles = ((entry.get("media") or {}).get("title") or {})
    name = titles.get("english") or titles.get("romaji")
    return name.strip()[:500] if isinstance(name, str) and name.strip() else None


async def fetch_window(
    client: httpx.AsyncClient, lookback_hours: int, horizon_days: int
) -> List[Tuple[int, int, datetime, Optional[str]]]:
    """``(anilist_id, episode, airing_at, title)`` for every anime airing between
    ``now - lookback`` and ``now + horizon``.

    Returns whatever it managed to collect on a failure: a partial window keeps
    the calendar mostly right until the next refresh, where raising would leave
    it empty.
    """
    now = utc_now()
    start = int((now - timedelta(hours=lookback_hours)).timestamp())
    end = int((now + timedelta(days=horizon_days)).timestamp())

    collected: List[Tuple[int, int, datetime, Optional[str]]] = []
    page = 1
    while page <= _MAX_PAGES:
        try:
            response = await anilist_post(
                client, _QUERY,
                {"page": page, "perPage": _PER_PAGE, "from": start, "to": end},
            )
        except Exception as e:
            logger.error(f"Airing schedule page {page} failed: {e}")
            break

        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            logger.error(f"Airing schedule page {page}: AniList status {status}")
            break

        # A GraphQL error still answers 200, with "data": null.
        try:
            payload = ((response.json() or {}).get("data") or {}).get("Page") or {}
        except ValueError:
            logger.error(f"Airing schedule page {page}: AniList sent a non-JSON body")
            break
        for entry in payload.get("airingSchedules") or []:
            media_id = entry.get("mediaId")
            episode = entry.get("episode")
            airing_at = entry.get("airingAt")
            if media_id is None or episode is None or airing_at is None:
                continue
            collected.append((
                int(media_id), int(episode),
                datetime.fromtimestamp(int(airing_at), tz=timezone.utc),
                _title(entry),
            ))

        if not (payload.get("pageInfo") or {}).get("hasNextPage"):
            break
        page += 1
    else:
        logger.warning(
            f"Airing schedule stopped at the {_MAX_PAGES}-page guard; "
            "the window is larger than expected"
        )

    logger.info(
        f"Airing schedule: {len(collected)} airing(s) across {page} page(s) "
        f"(-{lookback_hours}h to +{horizon_days}d)"
    )
    return collected
