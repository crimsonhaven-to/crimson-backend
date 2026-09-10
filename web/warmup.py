"""Continue-watching warmup: pre-cache the next episode after a progress save.

Looks ahead to the next episode, scrapes and resolves it in the background, and
hands the source closest to the viewer's language preference to the cache engine.
By the time they hit "next" it is already remuxed onto the NAS and plays
instantly.

api.py wires ``schedule_warmup`` into the account router. Everything here is
best-effort and fire-and-forget, and self-skips when caching is disabled.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

from fastapi.requests import Request
from starlette.concurrency import run_in_threadpool

from scrapers import ALL_SCRAPERS
from cache_engine.downloader import manager as cache_manager
from core.http_client import http_client
from metadata_engine.tmdb import _season_episode_info, fetch_tmdb_localized_titles, fetch_tmdb_show
from metadata_engine.anilist import fetch_anilist_metadata

from web.pipeline import resolve_streams, run_single_scraper
from web.queries import get_anilist_id, get_show_info
from web.util import _is_future_air_date, _public_base_url

logger = logging.getLogger("crimson.warmup")

# Progress posts fire every few seconds of playback, so repeats for one
# (show, season, episode) collapse into a single scrape window. The cache engine's
# DB claim dedupes the download regardless; this only spares redundant scraping.
_WARMUP_TTL = 900.0          # one warmup per next-episode per 15 min
_WARMUP_MAX = 5000           # bounds memory
_warmup_seen: Dict[str, float] = {}
# Strong refs, so the event loop doesn't GC an in-flight task mid-run.
_warmup_tasks: set = set()


async def _resolve_all_streams(tmdb_id: int, season_number: int, episode_number: int,
                               anilist_id: Optional[int], fallback_title: Optional[str],
                               base_url: str, media_type: str = "tv") -> List[Dict]:
    """Every resolvable stream for one episode, as a list: the non-progressive
    sibling of ``stream_watch_response``. Runs all scrapers concurrently, resolves
    their embeds and dedupes. A failing scraper is skipped.

    The media context is built exactly as ``stream_watch_response`` does, so the
    warmup resolves the same sources a real /watch call would. Keep them in sync."""
    anilist_data = {}
    if anilist_id:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id) or {}
    title = anilist_data.get("title") or fallback_title
    media_ctx = {**anilist_data, "title": title}
    if not anilist_id and media_type != "movie":
        try:
            async with http_client() as client:
                german_titles = await fetch_tmdb_localized_titles(client, tmdb_id)
            if german_titles:
                existing = list(media_ctx.get("synonyms") or [])
                media_ctx["synonyms"] = existing + [
                    t for t in german_titles if t not in existing
                ]
        except Exception as e:
            logger.warning(f"warmup localized-title enrichment failed for {tmdb_id}: {e}")

    seen_embeds: set = set()
    seen_urls: set = set()
    out: List[Dict] = []
    lock = asyncio.Lock()

    async def _work(scraper_class):
        try:
            embeds = await run_single_scraper(
                scraper_class, tmdb_id, season_number, episode_number, media_ctx,
                media_type=media_type,
            )
            for embed in embeds:
                if isinstance(embed, dict):
                    embed_url, language = embed.get("url"), embed.get("language")
                else:
                    embed_url, language = embed, None
                if not embed_url:
                    continue
                async with lock:
                    if embed_url in seen_embeds:
                        continue
                    seen_embeds.add(embed_url)
                for stream in await resolve_streams([embed_url], base_url=base_url, language=language):
                    async with lock:
                        if stream["url"] in seen_urls:
                            continue
                        seen_urls.add(stream["url"])
                        out.append(stream)
        except Exception as e:
            logger.error(f"warmup scraper error for {scraper_class.__name__}: {e}")

    await asyncio.gather(*(_work(sc) for sc in ALL_SCRAPERS), return_exceptions=True)
    return out


def _warmup_pick_best(streams: List[Dict], preferences: Optional[Dict]) -> Optional[Dict]:
    """Pick the stream the viewer would most likely auto-play, mirroring the
    frontend's ``streamRank``.

    Ranking is purely the viewer's language preference, with no source-quality or
    provider priority. Fewer mismatches wins, and ties (including no preference)
    fall back to list order: ``min`` is stable, so the earliest-resolved stream
    wins, matching the client's arrival-order fallback."""
    prefs = preferences or {}
    pref_lang = (prefs.get("language") or "").strip().lower()
    pref_type = (prefs.get("type") or "").strip().lower()

    def _mismatch(stream: Dict) -> int:
        if not pref_lang and not pref_type:
            return 0
        tag = (stream.get("language") or "").lower()
        miss = 0
        if pref_lang and pref_lang not in tag:
            miss += 1
        if pref_type and pref_type not in tag:
            miss += 1
        return miss

    if not streams:
        return None
    return min(streams, key=_mismatch)


async def _warmup_next_episode(*, base_url: str, tmdb_id: int, season_number: int,
                               episode_number: int, preferences: Optional[Dict]) -> None:
    """Scrape and resolve the episode after the one just watched, then hand the
    preference-closest cacheable source to the cache engine. Never raises, since
    it runs detached from the request."""
    try:
        if tmdb_id is None or season_number is None or episode_number is None:
            return
        # With caching off, resolving is wasted work, so bail before scraping.
        if not await run_in_threadpool(cache_manager._store.get_enabled):
            return

        next_ep = int(episode_number) + 1

        # One warmup per next-episode per window; see _warmup_seen.
        now = time.monotonic()
        key = f"{tmdb_id}:{season_number}:{next_ep}"
        seen_until = _warmup_seen.get(key)
        if seen_until is not None and seen_until > now:
            return
        if len(_warmup_seen) >= _WARMUP_MAX:
            _warmup_seen.clear()
        _warmup_seen[key] = now + _WARMUP_TTL

        # The next episode must exist in the season and have aired.
        info = await _season_episode_info(int(tmdb_id), int(season_number))
        air = info.get("air_dates") or {}
        if next_ep not in air:
            return  # end of season, or an unknown episode list
        if _is_future_air_date(air.get(next_ep)):
            return  # not out yet

        # Resolved the same way /watch does, off the same season, so the mapping is
        # identical. Falls back to a TMDB title for the title-based scrapers when
        # the season is not AniList-mapped.
        anilist_id = get_anilist_id(int(tmdb_id), int(season_number))
        fallback_title = None
        if not anilist_id:
            show = get_show_info(int(tmdb_id))
            fallback_title = show.get("title") if show else None
            if not fallback_title:
                try:
                    async with http_client() as client:
                        meta = await fetch_tmdb_show(client, int(tmdb_id))
                    fallback_title = meta.get("title")
                except Exception:
                    pass

        streams = await _resolve_all_streams(
            int(tmdb_id), int(season_number), next_ep, anilist_id,
            fallback_title, base_url=base_url, media_type="tv",
        )
        # Weigh only sources the cache engine would accept, so the pick is the
        # best *cacheable* match rather than one we would silently fail to cache.
        cacheable = [s for s in streams if await cache_manager._cacheable(s)]
        best = _warmup_pick_best(cacheable, preferences)
        if not best:
            return

        await cache_manager.maybe_enqueue(
            best,
            tmdb_id=int(tmdb_id),
            season_number=int(season_number),
            episode_number=next_ep,
            anilist_id=int(anilist_id) if anilist_id is not None else None,
            media_type="tv",
        )
        logger.info(
            f"warmup: queued next episode for caching tmdb={tmdb_id} "
            f"s{season_number}e{next_ep} source={best.get('source')!r} "
            f"lang={best.get('language')!r}"
        )
    except Exception as e:
        logger.warning(f"continue-watching warmup failed: {e}")


def schedule_warmup(request: Request, *, tmdb_id: int, season_number: int,
                    episode_number: int, preferences: Optional[Dict]) -> None:
    """The account router's hook: fire the warmup as a detached task and return
    immediately, so saving progress is never delayed by it. The public base URL is
    captured here, where the forwarded-header logic lives."""
    base_url = _public_base_url(request)
    task = asyncio.create_task(_warmup_next_episode(
        base_url=base_url, tmdb_id=tmdb_id, season_number=season_number,
        episode_number=episode_number, preferences=preferences,
    ))
    _warmup_tasks.add(task)
    task.add_done_callback(_warmup_tasks.discard)
