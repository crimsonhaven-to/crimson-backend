"""
The windowed AniList airing-schedule fetch.

Deliberately *not* ``fetch_anilist_metadata``: that is one title per request and
writes the shared response cache, so driving it from the poller would be one
round trip per subscription and would churn the cache every refresh. AniList's
``Page.airingSchedules`` answers the whole window at once, so the cost is a
handful of requests regardless of how many people follow how many shows.

It goes through ``anilist_post``, so it inherits the existing 429 ladder and
Retry-After handling rather than growing its own.

The window is not restricted to subscribed ids. Fetching everything that airs in
it costs the same handful of requests, and it is what lets the calendar show what
is airing this week rather than only the caller's own shows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx

from metadata_engine.anilist import anilist_post

logger = logging.getLogger("crimson.airing")

# AniList's ceiling for this connection.
_PER_PAGE = 50

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
    """The show's name as AniList gives it, English first.

    Worth carrying because the calendar's other source, anime_entries, is filled
    by the Fribb mapping resync and lags behind a new season: without this, the
    shows most worth following are the ones that render as a bare id."""
    titles = ((entry.get("media") or {}).get("title") or {})
    name = titles.get("english") or titles.get("romaji")
    return name.strip()[:500] if isinstance(name, str) and name.strip() else None


async def fetch_window(
    client: httpx.AsyncClient, lookback_hours: int, horizon_days: int
) -> List[Tuple[int, int, datetime, Optional[str]]]:
    """Every anime airing between ``now - lookback`` and ``now + horizon``.

    Returns ``(anilist_id, episode, airing_at, title)`` tuples ready for
    ``AiringStore.upsert_schedule``. Degrades to whatever it managed to collect:
    a partial window keeps the calendar mostly right and the next refresh fills
    the rest, where raising would leave it empty.
    """
    now = datetime.now(timezone.utc)
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
