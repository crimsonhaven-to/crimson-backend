"""Continue-watching warmup: pre-cache the NEXT episode after a progress save.

When a viewer saves progress on an episode, we look ahead to the NEXT one,
scrape+resolve it in the background, and hand the source closest to their
language/dub-sub preference to the cache engine — so by the time they hit "next"
it's already remuxed onto the NAS and plays instantly. The progress-upsert route
(account_engine) calls ``schedule_warmup`` via the injected handler; everything
here is best-effort and fire-and-forget, and self-skips when caching is disabled.

Lifted verbatim from ``api.py``; ``api.py`` wires ``schedule_warmup`` into the
account router with ``set_warmup_handler``.
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

# Don't re-scrape the same next-episode on every progress tick: progress posts fire
# every few seconds of playback, so collapse repeats for one (show, season, ep) into
# a single scrape window. The cache engine's DB claim dedupes the actual download
# regardless; this just spares the redundant scraping.
_WARMUP_TTL = 900.0          # seconds — one warmup per next-episode per 15 min
_WARMUP_MAX = 5000           # hard cap to bound memory
_warmup_seen: Dict[str, float] = {}
# Strong refs to in-flight warmup tasks so the event loop doesn't GC them mid-run.
_warmup_tasks: set = set()


async def _resolve_all_streams(tmdb_id: int, season_number: int, episode_number: int,
                               anilist_id: Optional[int], fallback_title: Optional[str],
                               base_url: str, media_type: str = "tv") -> List[Dict]:
    """Collect every resolvable stream for one episode into a list — a
    non-progressive sibling of ``stream_watch_response`` used by the warmup. Runs
    all scrapers concurrently, resolves their embeds, dedupes by embed/URL, and
    returns the streams. Best-effort: a failing scraper is skipped.

    The media-context build mirrors ``stream_watch_response`` (AniList metadata +
    German-title synonyms for the no-AniList path) so the warmup resolves the same
    sources the real /watch call would — kept deliberately in sync."""
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
    frontend ranker (crimson-client/src/hooks.js ``streamRank``): the language/
    dub-sub preference is the PRIMARY key (×1000), the global source priority
    (Cache > Voe > Jellyfin) is the tiebreaker within a language tier. Lower wins.
    With no preference set, source priority alone decides. Returns None for []."""
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

    def _priority(stream: Dict) -> int:
        if "/cache_proxy/" in (stream.get("url") or ""):
            return 0
        s = (stream.get("source") or "").lower()
        if "voe" in s:
            return 1
        if "jellyfin" in s:
            return 2
        return 100

    if not streams:
        return None
    return min(streams, key=lambda s: _mismatch(s) * 1000 + _priority(s))


async def _warmup_next_episode(*, base_url: str, tmdb_id: int, season_number: int,
                               episode_number: int, preferences: Optional[Dict]) -> None:
    """Scrape+resolve the episode after the one just watched and hand the
    preference-closest cacheable source to the cache engine. Fully best-effort;
    never raises (it runs detached from the request)."""
    try:
        if tmdb_id is None or season_number is None or episode_number is None:
            return
        # Caching off? Resolving would be wasted work — bail before any scraping.
        if not await run_in_threadpool(cache_manager._store.get_enabled):
            return

        next_ep = int(episode_number) + 1

        # TTL dedupe (see _warmup_seen): one warmup per next-episode per window.
        now = time.monotonic()
        key = f"{tmdb_id}:{season_number}:{next_ep}"
        seen_until = _warmup_seen.get(key)
        if seen_until is not None and seen_until > now:
            return
        if len(_warmup_seen) >= _WARMUP_MAX:
            _warmup_seen.clear()
        _warmup_seen[key] = now + _WARMUP_TTL

        # The next episode must actually exist in the season and already have aired.
        info = await _season_episode_info(int(tmdb_id), int(season_number))
        air = info.get("air_dates") or {}
        if next_ep not in air:
            return  # end of season (or unknown episode list) — nothing to warm
        if _is_future_air_date(air.get(next_ep)):
            return  # not out yet

        # Resolve the AniList mapping the same way /watch does (same season as the
        # episode just watched, so the mapping is identical). Falls back to a TMDB
        # title for the title-based scrapers when the season isn't AniList-mapped.
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
        # Only weigh sources the cache engine would actually accept (enabled +
        # ffmpeg present + tappable, non-self URL) so we pick the best *cacheable*
        # match rather than a source we'd silently fail to cache.
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
    """Account router's warmup hook: fire the warmup as a detached background task
    (keeping a strong ref so it isn't GC'd) and return immediately, so saving watch
    progress is never delayed by it. The public base URL is captured from the
    request here (where the forwarded-header logic lives) for the proxy sources."""
    base_url = _public_base_url(request)
    task = asyncio.create_task(_warmup_next_episode(
        base_url=base_url, tmdb_id=tmdb_id, season_number=season_number,
        episode_number=episode_number, preferences=preferences,
    ))
    _warmup_tasks.add(task)
    task.add_done_callback(_warmup_tasks.discard)
