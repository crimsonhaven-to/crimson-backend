"""Pre-cache the next episode after a progress save, so it plays instantly off
the NAS when the viewer moves on. Best effort, detached from the request, and a
no-op while caching is off."""

import asyncio
import logging
import time
from typing import Dict, List, Optional

from fastapi.requests import Request

from cache_engine.downloader import manager as cache_manager
from core.background import spawn
from core.http_client import http_client
from core.public_url import public_base_url
from metadata_engine import catalogue
from metadata_engine.dates import is_future_air_date
from metadata_engine.tmdb import fetch_tmdb_show, season_episode_info

from .pipeline import add_localized_titles, anilist_context, fan_out

logger = logging.getLogger("crimson.warmup")

# Progress saves fire every few seconds of playback. One warmup per next episode
# per window spares the repeated scraping; the cache's own claim dedupes the
# download regardless.
_WARMUP_TTL = 900.0
_WARMUP_MAX = 5000
_warmup_seen: Dict[str, float] = {}


def _pick_best(streams: List[Dict], preferences: Optional[Dict]) -> Optional[Dict]:
    """The stream the client would auto-play, mirroring its ``streamRank``: fewest
    mismatches against the preferred language and dub/sub type. ``min`` is
    stable, so ties keep arrival order, as the client does."""
    prefs = preferences or {}
    pref_lang = (prefs.get("language") or "").strip().lower()
    pref_type = (prefs.get("type") or "").strip().lower()

    def _mismatches(stream: Dict) -> int:
        tag = (stream.get("language") or "").lower()
        return (bool(pref_lang) and pref_lang not in tag) + (bool(pref_type) and pref_type not in tag)

    return min(streams, key=_mismatches) if streams else None


def _claim_window(key: str) -> bool:
    now = time.monotonic()
    if _warmup_seen.get(key, 0) > now:
        return False
    if len(_warmup_seen) >= _WARMUP_MAX:
        _warmup_seen.clear()
    _warmup_seen[key] = now + _WARMUP_TTL
    return True


async def _fallback_title(tmdb_id: int) -> Optional[str]:
    title = (await asyncio.to_thread(catalogue.get_show_info, tmdb_id)).get("title")
    if title:
        return title
    try:
        async with http_client() as client:
            return (await fetch_tmdb_show(client, tmdb_id)).get("title")
    except Exception:
        return None


async def _warmup_next_episode(*, base_url: str, tmdb_id: int, season_number: int,
                               episode_number: int, preferences: Optional[Dict]) -> None:
    try:
        if not await asyncio.to_thread(cache_manager.store.get_enabled):
            return
        next_ep = episode_number + 1
        if not _claim_window(f"{tmdb_id}:{season_number}:{next_ep}"):
            return

        air_dates = (await season_episode_info(tmdb_id, season_number)).get("air_dates") or {}
        if next_ep not in air_dates or is_future_air_date(air_dates.get(next_ep)):
            return  # end of season, or not out yet

        anilist_id = await asyncio.to_thread(catalogue.get_anilist_id, tmdb_id, season_number)
        media_ctx = await anilist_context(anilist_id, None if anilist_id else await _fallback_title(tmdb_id))
        if not anilist_id:
            await add_localized_titles(media_ctx, tmdb_id)

        streams = [s async for s in fan_out(media_ctx, tmdb_id, season_number, next_ep, base_url, "tv")]
        # Only sources the cache would accept, so the pick is never one that
        # would silently fail to cache.
        best = _pick_best([s for s in streams if await cache_manager.cacheable(s)], preferences)
        if not best:
            return
        await cache_manager.maybe_enqueue(
            best, tmdb_id=tmdb_id, season_number=season_number, episode_number=next_ep,
            anilist_id=anilist_id, media_type="tv",
        )
        logger.info(
            f"warmup: queued tmdb={tmdb_id} s{season_number}e{next_ep} "
            f"source={best.get('source')!r} lang={best.get('language')!r}"
        )
    except Exception as e:
        logger.warning(f"continue-watching warmup failed: {e}")


def schedule_warmup(request: Request, *, tmdb_id: int, season_number: int,
                    episode_number: int, preferences: Optional[Dict]) -> None:
    """Fire and forget, so saving progress is never delayed. The base URL is read
    here, while the request is still at hand."""
    spawn(_warmup_next_episode(
        base_url=public_base_url(request), tmdb_id=int(tmdb_id), season_number=int(season_number),
        episode_number=int(episode_number), preferences=preferences,
    ))
