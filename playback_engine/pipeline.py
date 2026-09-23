"""Scrape and resolve an episode into playable streams.

Every scraper runs at once, each embed it finds is resolved to a stream, and
streams are deduplicated across sources. ``watch_events`` wraps that in the
/watch contract (meta, streams as they land, done), which the NDJSON route, the
movie-web bridge and the warmup all consume.
"""

import asyncio
import logging
import time
from typing import AsyncIterator, Dict, List, Optional

from cache_engine.downloader import manager as cache_manager
from core import metrics
from core.contracts import build_done_line, build_meta_line, build_stream_line, build_unaired_line
from core.http_client import http_client
from metadata_engine.anilist import fetch_anilist_metadata
from metadata_engine.dates import is_future_air_date
from metadata_engine.tmdb import fetch_tmdb_localized_titles, season_episode_info
from resolvers import ALL_RESOLVERS
from scrapers import ALL_SCRAPERS

logger = logging.getLogger("crimson.pipeline")


async def run_single_scraper(
    scraper_class,
    tmdb_id: int,
    season_num: int,
    episode_num: int,
    anilist_data: Dict,
    media_type: str = "tv",
) -> List:
    """One scraper's embeds for an episode. Episode-only sources are skipped for
    a movie, so they never build a bogus season 1 episode 1 URL for a film."""
    if media_type == "movie" and not getattr(scraper_class, "SUPPORTS_MOVIES", False):
        return []
    scraper = scraper_class()
    # "empty" (answered, found nothing) and "error" (threw) mean different things
    # on a dashboard: a title-matching miss versus a source gone dark.
    started = time.monotonic()
    outcome = "error"
    try:
        media_ctx = {
            "tmdb_id": tmdb_id,
            "tmdb_season": season_num,
            "media_type": media_type,
            **anilist_data,
        }
        slug = await scraper.search_anime(media_ctx)
        if not slug:
            outcome = "empty"
            return []
        embeds = await scraper.get_episode_embeds(slug, episode_num, season_num)
        outcome = "embeds" if embeds else "empty"
        return embeds
    except Exception as e:
        logger.error(f"Scraper error for {scraper_class.__name__}: {e}")
        return []
    finally:
        metrics.record_scraper_run(scraper_class.__name__, outcome, time.monotonic() - started)
        await scraper.close()


def _absolute(url: str, base_url: str) -> str:
    """Proxy paths are same-origin to this backend, and the client is another
    origin, so they are made absolute."""
    return base_url.rstrip("/") + url if url.startswith("/") and base_url else url


def _stream(item: Dict, default_source: str, base_url: str) -> Dict:
    """Shape one resolved stream. A backend-hosted player page (an overlay's
    ``/x_proxy/h/...`` or ``/player``) is iframed; anything else plays as HLS or
    MP4, decided by the URL rather than the mutable source label."""
    url = item["url"]
    if "_proxy/h/" in url or url.startswith("/player"):
        kind = "iframe"
    else:
        kind = item.get("type") or ("hls" if "m3u8" in url.lower() else "mp4")
    stream = {
        "source": item.get("source") or default_source,
        "type": kind,
        "url": _absolute(url, base_url),
    }
    subtitles = [
        {**s, "url": _absolute(s["url"], base_url)} if isinstance(s.get("url"), str) else s
        for s in item.get("subtitles") or []
    ]
    if subtitles:
        stream["subtitles"] = subtitles
    if item.get("language"):
        stream["language"] = item["language"]
    return stream


def _as_items(resolved) -> List[Dict]:
    """A resolver answers with a URL, a dict carrying one (plus a label such as
    the cache target's name), or a list of dicts when one embed fans out into
    several variants."""
    if isinstance(resolved, list):
        return [r for r in resolved if isinstance(r, dict) and r.get("url")]
    if isinstance(resolved, dict):
        return [resolved] if resolved.get("url") else []
    return [{"url": resolved}] if resolved else []


async def resolve_streams(
    embed_urls: List[str], base_url: str = "", language: Optional[str] = None
) -> List[Dict]:
    """Resolve embeds to streams. ``language`` is the dub or sub label a scraper
    knew for these embeds, stamped on every stream they produce."""
    resolvers = [cls() for cls in ALL_RESOLVERS]
    streams: List[Dict] = []
    for embed_url in embed_urls:
        resolver = next((r for r in resolvers if r.domain_keyword in embed_url.lower()), None)
        if resolver is None:
            streams.append({"source": "Direct Embed", "type": "iframe", "url": embed_url})
            continue

        started = time.monotonic()
        try:
            resolved = await resolver.resolve(embed_url)
        except Exception as e:
            # Dropped rather than shown as a dead tile, which used to race to the
            # top of the list because it failed fast.
            metrics.record_resolve(resolver.source_name, "error", time.monotonic() - started)
            logger.error(f"Resolver error for {resolver.source_name}: {e}")
            continue
        metrics.record_resolve(
            resolver.source_name, "ok" if resolved else "empty", time.monotonic() - started
        )

        items = _as_items(resolved)
        if not items and not isinstance(resolved, list):
            # For marker-based sources the embed is a routing token, not a page,
            # and iframing it would show an empty frame.
            if embed_url.lower().startswith(("http://", "https://")):
                streams.append(
                    {
                        "source": f"{resolver.source_name} (Embed)",
                        "type": "iframe",
                        "url": embed_url,
                    }
                )
            continue
        streams.extend(_stream(item, resolver.source_name, base_url) for item in items)

    if language:
        for stream in streams:
            stream["language"] = language
    return streams


async def anilist_context(anilist_id: Optional[int], fallback_title: Optional[str]) -> Dict:
    anilist_data = {}
    if anilist_id:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id) or {}
    return {**anilist_data, "title": anilist_data.get("title") or fallback_title}


async def add_localized_titles(media_ctx: Dict, tmdb_id: int) -> None:
    """German sites list many non-anime shows under their German broadcast title,
    which TMDB only exposes via /translations. Mapped anime already carry their
    AniList synonyms, so this is only for unmapped TV."""
    try:
        async with http_client() as client:
            german = await fetch_tmdb_localized_titles(client, tmdb_id)
    except Exception as e:
        logger.warning(f"localized-title enrichment failed for {tmdb_id}: {e}")
        return
    existing = list(media_ctx.get("synonyms") or [])
    media_ctx["synonyms"] = existing + [t for t in german or [] if t not in existing]


async def fan_out(
    media_ctx: Dict, tmdb_id: int, season_number, episode_number, base_url: str, media_type: str
) -> AsyncIterator[Dict]:
    """Yield each distinct stream the moment its scraper and resolver finish, so
    a slow source never holds back a fast one. Closing the iterator cancels the
    remaining scrapers."""
    queue: asyncio.Queue = asyncio.Queue()
    seen_embeds: set = set()
    seen_urls: set = set()

    async def _work(scraper_class):
        try:
            embeds = await run_single_scraper(
                scraper_class,
                tmdb_id,
                season_number,
                episode_number,
                media_ctx,
                media_type=media_type,
            )
            for embed in embeds:
                # A bare URL, or a dict when the scraper knows the dub or sub.
                embed_url, language = (
                    (embed.get("url"), embed.get("language"))
                    if isinstance(embed, dict)
                    else (embed, None)
                )
                if not embed_url or embed_url in seen_embeds:
                    continue
                seen_embeds.add(embed_url)
                for stream in await resolve_streams(
                    [embed_url], base_url=base_url, language=language
                ):
                    if stream["url"] not in seen_urls:
                        seen_urls.add(stream["url"])
                        await queue.put(stream)
        except Exception as e:
            logger.error(f"Streaming scraper error for {scraper_class.__name__}: {e}")

    workers = [asyncio.create_task(_work(cls)) for cls in ALL_SCRAPERS]

    async def _finish():
        await asyncio.gather(*workers, return_exceptions=True)
        await queue.put(None)

    finisher = asyncio.create_task(_finish())
    try:
        while (stream := await queue.get()) is not None:
            yield stream
    finally:
        finisher.cancel()
        for worker in workers:
            worker.cancel()


async def watch_events(
    tmdb_id: int,
    season_number,
    episode_number,
    anilist_id: Optional[int],
    fallback_title: Optional[str] = None,
    base_url: str = "",
    media_type: str = "tv",
) -> AsyncIterator[Dict]:
    """The /watch contract as dicts: ``meta`` at once, ``unaired`` for an episode
    not out yet, one ``stream`` per source as it lands, then ``done``.

    A movie has no season, episode or AniList mapping, so it skips the air-date
    check and the localized titles, and only movie-capable sources run."""
    # Timed from here, so time-to-first-stream is what the viewer actually waits.
    started = time.monotonic()
    media_ctx = await anilist_context(anilist_id, fallback_title)
    yield build_meta_line(
        tmdb_id=tmdb_id,
        season_number=season_number,
        episode_number=episode_number,
        anilist_id=anilist_id,
        title=media_ctx["title"],
    )

    if media_type != "movie":
        # Extras are not in the numbered-season list, so they carry no air date.
        air_date = ((await season_episode_info(tmdb_id, season_number)).get("air_dates") or {}).get(
            episode_number
        )
        if is_future_air_date(air_date):
            metrics.record_watch(media_type, "unaired", 0, time.monotonic() - started)
            yield build_unaired_line(
                air_date=air_date,
                title=media_ctx["title"],
                season_number=season_number,
                episode_number=episode_number,
            )
            yield build_done_line(0)
            return
        if not anilist_id:
            await add_localized_titles(media_ctx, tmdb_id)

    count = 0
    first_stream: Optional[float] = None
    finished = False
    try:
        async for stream in fan_out(
            media_ctx, tmdb_id, season_number, episode_number, base_url, media_type
        ):
            # Caching on resolve would cache whichever source was fastest, so the
            # stream carries a signed ticket the player redeems after about ten
            # seconds of playback instead.
            stream["cacheTicket"] = await cache_manager.mint_ticket(
                stream,
                tmdb_id=tmdb_id,
                season_number=season_number if season_number is not None else 0,
                episode_number=episode_number if episode_number is not None else 0,
                anilist_id=anilist_id,
                media_type=media_type,
            )
            count += 1
            if first_stream is None:
                first_stream = time.monotonic() - started
            yield build_stream_line(stream)
        # Recorded before the done line, so a consumer that leaves at the very end
        # still leaves an accurate sample.
        metrics.record_watch(
            media_type,
            "streams" if count else "empty",
            count,
            time.monotonic() - started,
            first_stream,
        )
        finished = True
        yield build_done_line(count)
    finally:
        if not finished:
            # Left before the fan-out finished: abandonment, never failure.
            metrics.record_watch(
                media_type, "abandoned", count, time.monotonic() - started, first_stream
            )
