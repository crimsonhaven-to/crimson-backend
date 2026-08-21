"""The scrape/resolve engine and the progressive NDJSON /watch stream.

The heart of the operator-owned playback path (the E0 floor of the New System):
run every scraper, resolve its embeds to direct streams, dedupe, and emit one
NDJSON line per source as it lands.
"""

import asyncio
import logging
from typing import Dict, List, Optional

from scrapers import ALL_SCRAPERS
from resolvers import ALL_RESOLVERS
from core.http_client import http_client
from core import observability
from core.contracts import (
    build_done_line,
    build_meta_line,
    build_stream_line,
    build_unaired_line,
)
from cache_engine.downloader import manager as cache_manager
from metadata_engine.tmdb import _season_episode_info, fetch_tmdb_localized_titles
from metadata_engine.anilist import fetch_anilist_metadata

from web.util import _is_future_air_date, _ndjson, _STREAM_HEADERS

logger = logging.getLogger("crimson.pipeline")

__all__ = [
    "run_single_scraper",
    "resolve_streams",
    "stream_watch_response",
    "_enrich_progress_rows",
    "_STREAM_HEADERS",
]


async def run_single_scraper(scraper_class, tmdb_id: int, season_num: int, episode_num: int,
                             anilist_data: Dict, media_type: str = "tv") -> List:
    """Run one scraper through the unified search-to-embeds pipeline.

    Scrapers that don't declare ``SUPPORTS_MOVIES`` are skipped for movie
    requests, so the episode-oriented anime sources never build a bogus
    season-1/episode-1 URL for a standalone film."""
    if media_type == "movie" and not getattr(scraper_class, "SUPPORTS_MOVIES", False):
        return []
    scraper = scraper_class()
    # The outcomes are worth telling apart on a dashboard: "empty" answered but
    # found nothing, a title-matching miss or a source gone dark, while "error"
    # threw. Best-effort and cannot raise.
    timer = observability.Timer()
    outcome = "error"
    try:
        media_ctx = {
            "tmdb_id": tmdb_id,
            "tmdb_season": season_num,
            "media_type": media_type,
            **anilist_data
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
        observability.record_scraper_run(
            scraper_class.__name__, outcome, timer.lap()
        )
        await scraper.close()


async def resolve_streams(embed_urls: List[str], base_url: str = "", language: Optional[str] = None) -> List[Dict]:
    """Resolve embed URLs to direct stream URLs.

    ``base_url`` is this backend's public base, used to turn a resolver's relative
    proxy or player path into an absolute URL the frontend can load.

    ``language`` is an audio/subtitle label some sources know and others don't.
    When set it is stamped onto every resolved stream; otherwise it stays blank.
    """
    if not embed_urls:
        return []

    resolver_instances = [resolver_class() for resolver_class in ALL_RESOLVERS]
    resolved_streams = []

    for embed_url in embed_urls:
        matched_resolver = None
        for resolver in resolver_instances:
            if resolver.domain_keyword in embed_url.lower():
                matched_resolver = resolver
                break

        if matched_resolver:
            # Times the resolve hop only, not the formatting below, so the metric
            # measures the upstream. ``recorded`` stops a later formatting failure
            # being counted a second time as a resolver error.
            timer = observability.Timer()
            recorded = False
            try:
                resolved = await matched_resolver.resolve(embed_url)
                observability.record_resolve(
                    matched_resolver.source_name,
                    "ok" if resolved else "empty",
                    timer.lap(),
                )
                recorded = True
                # A resolver returns a list of already-formed stream dicts when
                # one marker fans out to several variants, such as a server's
                # qualities. Each becomes its own tile.
                if isinstance(resolved, list):
                    for item in resolved:
                        if not isinstance(item, dict) or not item.get("url"):
                            continue
                        item_url = item["url"]
                        if item_url.startswith("/") and base_url:
                            item_url = base_url.rstrip("/") + item_url
                        item_subs = item.get("subtitles") or None
                        if item_subs and base_url:
                            item_subs = [
                                {**s, "url": base_url.rstrip("/") + s["url"]}
                                if isinstance(s.get("url"), str) and s["url"].startswith("/")
                                else s
                                for s in item_subs
                            ]
                        stream_obj = {
                            "source": item.get("source") or matched_resolver.source_name,
                            "type": item.get("type")
                            or ("hls" if "m3u8" in item_url.lower() else "mp4"),
                            "url": item_url,
                        }
                        if item_subs:
                            stream_obj["subtitles"] = item_subs
                        if item.get("language"):
                            stream_obj["language"] = item["language"]
                        resolved_streams.append(stream_obj)
                    continue
                subtitles = None
                source_override = None
                if isinstance(resolved, dict):
                    subtitles = resolved.get("subtitles") or None
                    # A resolver may override the label per stream, as the Cache
                    # source does with its NAS target's name.
                    source_override = resolved.get("source") or None
                    direct_video_url = resolved.get("url")
                else:
                    direct_video_url = resolved
                # Subtitle URLs are same-origin proxy paths too.
                if subtitles and base_url:
                    subtitles = [
                        {**s, "url": base_url.rstrip("/") + s["url"]}
                        if isinstance(s.get("url"), str) and s["url"].startswith("/")
                        else s
                        for s in subtitles
                    ]
                if direct_video_url:
                    # Shape is decided by the returned URL, not by source_name,
                    # which is a mutable display label:
                    #   /{x}_proxy/h/...    ad-stripped player page, so iframe it
                    #   /jellyfin_proxy/... a proxied raw stream, so hls/mp4
                    #   anything relative   made absolute against the backend base,
                    #                       since the frontend is another origin
                    # An absolute third-party URL falls through to hls/mp4.
                    is_proxy_path = direct_video_url.startswith("/")
                    abs_url = direct_video_url
                    if is_proxy_path and base_url:
                        abs_url = base_url.rstrip("/") + direct_video_url
                    source_label = source_override or matched_resolver.source_name

                    if "_proxy/h/" in direct_video_url or direct_video_url.startswith("/player"):
                        # A backend-hosted player page, which the frontend iframes.
                        resolved_streams.append({
                            "source": source_label,
                            "type": "iframe",
                            "url": abs_url
                        })
                    else:
                        stream_type = "hls" if "m3u8" in direct_video_url.lower() else "mp4"
                        stream_obj = {
                            "source": source_label,
                            "type": stream_type,
                            "url": abs_url
                        }
                        if subtitles:
                            stream_obj["subtitles"] = subtitles
                        resolved_streams.append(stream_obj)
                else:
                    # Nothing playable. Only fall back to iframing embed_url when
                    # it is a genuine http(s) page. For marker-based sources it is
                    # an internal routing token, not a URL, and iframing it yields
                    # an empty src the frontend's CSP blocks, so drop the source
                    # rather than surface a dead tile.
                    if embed_url.lower().startswith(("http://", "https://")):
                        resolved_streams.append({
                            "source": f"{matched_resolver.source_name} (Embed)",
                            "type": "iframe",
                            "url": embed_url
                        })
                    else:
                        logger.info(
                            f"{matched_resolver.source_name}: no stream for marker "
                            f"{embed_url!r}; dropping (not a frameable URL)."
                        )
                        continue
            except Exception as e:
                # Drop an erroring resolver entirely rather than emit a broken
                # "(Error)" iframe: that placeholder used to surface as a dead
                # source, and a fast-failing one raced to the top of the list.
                if not recorded:
                    observability.record_resolve(
                        matched_resolver.source_name, "error", timer.lap()
                    )
                logger.error(f"Resolver error for {matched_resolver.source_name}: {e}")
                continue
        else:
            resolved_streams.append({
                "source": "Direct Embed",
                "type": "iframe",
                "url": embed_url
            })

    # Every embed in one call shares a language. Left off entirely when unknown.
    if language:
        for stream in resolved_streams:
            stream["language"] = language

    return resolved_streams


async def stream_watch_response(tmdb_id: int, season_number: int, episode_number: int,
                                anilist_id: Optional[int], fallback_title: Optional[str] = None,
                                base_url: str = "", media_type: str = "tv"):
    """Scrape and resolve an episode, yielding NDJSON lines as each source lands
    rather than waiting for every scraper.

    For a movie there is no season, episode or AniList mapping, so the air-date,
    localized-title and cache-ticket steps are skipped and only the movie-capable
    sources run.

    Emits, in order: one ``meta`` line flushed immediately; one ``stream`` line
    per resolved stream the instant its scraper and resolver finish, so the
    fastest source reaches the player first; and a final ``done`` line.

    Works without an AniList mapping, as TMDB-only seasons of long shows have.
    The TMDB-keyed sources play off the TMDB id and title-based scrapers fall back
    to the TMDB show title.
    """
    # Started here, not at the fan-out, so time-to-first-stream measures what the
    # viewer actually waits through, metadata fetch included.
    watch_timer = observability.Timer()

    anilist_data = {}
    if anilist_id:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id) or {}

    title = anilist_data.get("title") or fallback_title
    media_ctx = {**anilist_data, "title": title}

    yield _ndjson(build_meta_line(
        tmdb_id=tmdb_id,
        season_number=season_number,
        episode_number=episode_number,
        anilist_id=anilist_id,
        title=title,
    ))

    # Racing every scraper to resolve zero sources is wasted work, so a
    # future-dated episode returns the "not yet aired" state instead. Extras are
    # not in the numbered-season list, so they carry no air_date and play
    # normally, and movies have no episode list at all.
    if media_type != "movie":
        ep_info = await _season_episode_info(tmdb_id, season_number)
        air_date = (ep_info.get("air_dates") or {}).get(episode_number)
        if _is_future_air_date(air_date):
            observability.record_watch(media_type, "unaired", 0, watch_timer.lap())
            yield _ndjson(build_unaired_line(
                air_date=air_date,
                title=title,
                season_number=season_number,
                episode_number=episode_number,
            ))
            yield _ndjson(build_done_line(0))
            return

    # German scrapers list many non-anime shows under their German broadcast
    # title, which TMDB exposes only via /translations, so English-title matching
    # alone misses them. Only on the no-AniList path, since mapped anime carry
    # their own synonyms. Skipped for movies, whose sources are TMDB-id keyed.
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
            logger.warning(f"localized-title enrichment failed for {tmdb_id}: {e}")

    # Each scraper is its own task pushing resolved streams onto a queue as they
    # are ready, so a slow source never holds back a fast one. A lock-guarded
    # seen-set dedupes embeds and stream URLs across the concurrent sources.
    queue: asyncio.Queue = asyncio.Queue()
    seen_embeds: set = set()
    seen_urls: set = set()
    lock = asyncio.Lock()

    async def _work(scraper_class):
        try:
            embeds = await run_single_scraper(
                scraper_class, tmdb_id, season_number, episode_number, media_ctx,
                media_type=media_type,
            )
            for embed in embeds:
                # An embed is a bare URL, or a dict when the scraper knows the
                # dub/sub language.
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
                    # Downloading on resolve would always cache whichever source
                    # resolved fastest rather than the one the viewer picks, so
                    # cacheable streams get a signed ticket instead. The player
                    # redeems it after about 10s of real playback, and only then
                    # is the download enqueued. mint_ticket owns the policy and
                    # returns None for a movie, so no branch is needed here.
                    stream["cacheTicket"] = await cache_manager.mint_ticket(
                        stream,
                        tmdb_id=tmdb_id,
                        season_number=season_number if season_number is not None else 0,
                        episode_number=episode_number if episode_number is not None else 0,
                        anilist_id=anilist_id,
                        media_type=media_type,
                    )
                    await queue.put(stream)
        except Exception as e:
            logger.error(f"Streaming scraper error for {scraper_class.__name__}: {e}")

    workers = [asyncio.create_task(_work(sc)) for sc in ALL_SCRAPERS]

    async def _finish():
        # The sentinel ends the drain loop below.
        await asyncio.gather(*workers, return_exceptions=True)
        await queue.put(None)

    finisher = asyncio.create_task(_finish())

    count = 0
    first_stream: Optional[float] = None
    recorded = False
    try:
        while True:
            stream = await queue.get()
            if stream is None:  # every scraper finished
                break
            count += 1
            if first_stream is None:
                # How long the viewer stared at a spinner before anything became
                # playable, which is the number that matters operationally.
                first_stream = watch_timer.lap()
            # The shape lives in core.contracts so it cannot drift from the
            # client or crimson-sources.
            yield _ndjson(build_stream_line(stream))
        # Before the done line, so a consumer that leaves at the very end still
        # leaves an accurate sample behind.
        observability.record_watch(
            media_type, "streams" if count else "empty",
            count, watch_timer.lap(), first_stream,
        )
        recorded = True
        yield _ndjson(build_done_line(count))
    finally:
        # A mid-stream disconnect closes the generator here, so cancel the tasks
        # rather than leak them.
        if not recorded:
            # The viewer left, or picked a source and stopped reading, before the
            # fan-out finished. Counted separately so abandonment is never
            # mistaken for failure.
            observability.record_watch(
                media_type, "abandoned", count, watch_timer.lap(), first_stream,
            )
        finisher.cancel()
        for w in workers:
            w.cancel()


async def _enrich_progress_rows(rows: List[Dict]) -> None:
    """Attach "next episode" hints to deduped progress rows, so the frontend never
    offers an episode that does not exist or has not aired.

    Each row is one show at its latest watched season and episode. That season's
    TMDB episode list is looked up and adds, in place, ``season_episode_count``,
    ``next_episode_exists`` and ``next_episode_air_date``.

    Best-effort and concurrency-bounded: a per-row failure leaves that row
    unannotated and the frontend falls back to its old behaviour."""
    sem = asyncio.Semaphore(8)

    async def _one(row: Dict) -> None:
        tmdb_id, season = row.get("tmdb_id"), row.get("season_number")
        ep = row.get("episode_number")
        if not tmdb_id or season is None:
            return
        async with sem:
            info = await _season_episode_info(int(tmdb_id), int(season))
        if not info:
            return
        row["season_episode_count"] = info.get("count")
        if ep is not None:
            air = info.get("air_dates") or {}
            nxt = int(ep) + 1
            row["next_episode_exists"] = nxt in air
            row["next_episode_air_date"] = air.get(nxt)

    await asyncio.gather(*(_one(r) for r in rows), return_exceptions=True)
