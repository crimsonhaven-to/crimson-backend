"""The scrape -> resolve engine and the progressive NDJSON /watch stream.

This is the heart of the backend's operator-owned playback path (the E0 floor of
the New System): run every scraper, resolve its embeds to direct streams, dedupe,
and emit one NDJSON line per source as it lands. Lifted verbatim out of ``api.py``
so the watch/mw routes, the warmup handler and the account progress-enricher can
all share it without importing ``api.py``.
"""

import asyncio
import logging
from typing import Dict, List, Optional

from scrapers import ALL_SCRAPERS
from resolvers import ALL_RESOLVERS
from core.http_client import http_client
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
    """Run one scraper through the unified search -> embeds pipeline.

    ``media_type`` is "tv" (the default — every existing caller) or "movie".
    Scrapers that don't declare ``SUPPORTS_MOVIES`` are skipped for movie requests
    so the title/episode-oriented anime sources never build a bogus
    season-1/episode-1 URL for a standalone film."""
    if media_type == "movie" and not getattr(scraper_class, "SUPPORTS_MOVIES", False):
        return []
    scraper = scraper_class()
    try:
        media_ctx = {
            "tmdb_id": tmdb_id,
            "tmdb_season": season_num,
            "media_type": media_type,
            **anilist_data
        }
        slug = await scraper.search_anime(media_ctx)
        if not slug:
            return []
        return await scraper.get_episode_embeds(slug, episode_num, season_num)
    except Exception as e:
        logger.error(f"Scraper error for {scraper_class.__name__}: {e}")
        return []
    finally:
        await scraper.close()


async def resolve_streams(embed_urls: List[str], base_url: str = "", language: Optional[str] = None) -> List[Dict]:
    """Resolve embed URLs to direct stream URLs.

    ``base_url`` is the public base of this backend (e.g. https://host/). It is
    used to turn a resolver's relative proxy/player path (Jellyfin, local, cache)
    into an absolute stream URL the frontend can load.

    ``language`` is an optional human-readable audio/subtitle label (e.g. the NAS
    target's dub language), known by some sources (the cache) and not others. When
    set, it is stamped onto every resolved stream so the frontend can show it;
    otherwise the streams carry no language and it stays blank.
    """
    if not embed_urls:
        return []

    resolver_instances = [resolver_class() for resolver_class in ALL_RESOLVERS]
    resolved_streams = []

    for embed_url in embed_urls:
        # Find matching resolver
        matched_resolver = None
        for resolver in resolver_instances:
            if resolver.domain_keyword in embed_url.lower():
                matched_resolver = resolver
                break

        if matched_resolver:
            try:
                resolved = await matched_resolver.resolve(embed_url)
                # A resolver may return a LIST of already-formed stream dicts
                # ({"url", "source", "type", optional "language"/"subtitles"}) when
                # one marker fans out to many variants (ScreenScape: a server's
                # qualities/languages). Absolutize any same-origin proxy paths and
                # append each as its own tile.
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
                    # A resolver may override the display label per-stream (the
                    # Cache source labels each stream with its NAS target's name).
                    source_override = resolved.get("source") or None
                    direct_video_url = resolved.get("url")
                else:
                    direct_video_url = resolved
                # Subtitle URLs are same-origin proxy paths too — absolutize them
                # against the backend base like the main stream URL.
                if subtitles and base_url:
                    subtitles = [
                        {**s, "url": base_url.rstrip("/") + s["url"]}
                        if isinstance(s.get("url"), str) and s["url"].startswith("/")
                        else s
                        for s in subtitles
                    ]
                if direct_video_url:
                    # Decide the stream's shape by the URL the resolver returned,
                    # NOT by source_name (which is a mutable display label):
                    #   * /{x}_proxy/h/...  -> ad-stripped player-page proxy
                    #     (Movish) -> iframe the backend page.
                    #   * /jellyfin_proxy/... -> a proxied raw stream -> hls/mp4.
                    #   * anything relative ("/..") is made absolute against the
                    #     backend base so the frontend (a different origin) loads
                    #     it from us.
                    # Resolvers that hand back an absolute third-party URL fall
                    # through to the generic hls/mp4 branch.
                    is_proxy_path = direct_video_url.startswith("/")
                    abs_url = direct_video_url
                    if is_proxy_path and base_url:
                        abs_url = base_url.rstrip("/") + direct_video_url
                    source_label = source_override or matched_resolver.source_name

                    if "_proxy/h/" in direct_video_url or direct_video_url.startswith("/player"):
                        # Backend-hosted player page (Movish player-proxy, or our
                        # /player wrapping a Jellyfin/PlayIMDb/AnimeSuge stream):
                        # the frontend just iframes it.
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
                    # resolve() found nothing playable. Only fall back to
                    # iframing the raw embed_url if it's a genuine http(s) embed
                    # page (legacy resolvers). For marker-based sources
                    # (crimson-playimdb:..., crimson-animesuge:..., etc.) the
                    # embed_url is an INTERNAL routing token, not a URL — iframing
                    # it yields an empty frame src that the frontend's
                    # `frame-src https:` CSP blocks ("This content is blocked").
                    # Drop the source instead so it never surfaces as a dead tile.
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
                # A resolver that errors out has nothing playable to offer. Drop
                # it entirely instead of emitting a broken "(Error)" iframe — that
                # placeholder used to surface as a dead source (e.g. Movish, which
                # fails fast and so raced to the top of the list). Just log it.
                logger.error(f"Resolver error for {matched_resolver.source_name}: {e}")
                continue
        else:
            resolved_streams.append({
                "source": "Direct Embed",
                "type": "iframe",
                "url": embed_url
            })

    # Stamp the known language onto every stream from this batch (all embeds in a
    # single call share one language). Left off entirely when unknown.
    if language:
        for stream in resolved_streams:
            stream["language"] = language

    return resolved_streams


async def stream_watch_response(tmdb_id: int, season_number: int, episode_number: int,
                                anilist_id: Optional[int], fallback_title: Optional[str] = None,
                                base_url: str = "", media_type: str = "tv"):
    """Progressively scrape + resolve an episode, yielding NDJSON lines as each
    source is found — instead of waiting for every scraper to finish.

    ``media_type`` is "tv" (every existing caller) or "movie". For a movie there's
    no season/episode and no AniList mapping: the air-date / localized-title /
    cache-ticket steps (all TV/episode concepts) are skipped, and only the
    movie-capable TMDB-keyed sources run (see run_single_scraper).

    Emits, in order:
      * one ``{"type": "meta", ...}`` line (ids + title), flushed immediately;
      * one ``{"type": "stream", source, streamType, url}`` line per resolved
        stream, the instant its scraper + resolver finish — the sources race, so
        the fastest one reaches the player first;
      * a final ``{"type": "done", "count": N}`` line once every scraper is done.

    Works without an AniList mapping (e.g. TMDB-only seasons of long shows): the
    TMDB-keyed sources play off the TMDB id, and title-based scrapers fall back to
    the TMDB show title. ``base_url`` (the backend's public base) is threaded into stream
    resolution so the proxy sources can emit an absolute iframe URL.
    """
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

    # Don't waste scraper work on an episode that hasn't aired yet. TMDB carries a
    # per-episode air_date; when the requested episode is dated in the future, tell
    # the client to render a "not yet aired" state instead of racing every scraper
    # only to resolve zero sources. Extras (specials/OVAs/movies) aren't in the
    # numbered-season episode list, so they have no air_date here and play normally.
    # Movies have no episode list at all — skip the check entirely.
    if media_type != "movie":
        ep_info = await _season_episode_info(tmdb_id, season_number)
        air_date = (ep_info.get("air_dates") or {}).get(episode_number)
        if _is_future_air_date(air_date):
            yield _ndjson(build_unaired_line(
                air_date=air_date,
                title=title,
                season_number=season_number,
                episode_number=episode_number,
            ))
            yield _ndjson(build_done_line(0))
            return

    # German streaming scrapers (s.to, aniworld) list many non-anime shows under
    # their German broadcast title — e.g. NCIS is "Navy CIS" on s.to — which TMDB
    # only exposes via /translations, so English-title matching alone misses them.
    # Feed the German title(s) in as extra search candidates. Only on the
    # no-AniList path: AniList-mapped anime already carry their own synonyms and
    # that matching stays byte-identical. Skipped for movies (that endpoint is the
    # TV /translations entity; the movie sources are TMDB-id keyed anyway).
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

    # Each scraper runs as its own task: scrape -> resolve -> push the resolved
    # streams onto a queue the moment they're ready, so a slow source never holds
    # back a fast one. A shared seen-set (guarded by a lock) dedupes embeds and
    # stream URLs across sources, preserving the old global de-dup behaviour while
    # the work happens concurrently.
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
                # Embeds are either a bare URL string or a {"url", "language"}
                # dict (scrapers that know the dub/sub language, e.g. aniworld).
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
                    # Server-side cache: don't download on resolve (that always
                    # caches whichever source resolves fastest, not the one the
                    # viewer picks). Instead stamp cacheable streams with a signed
                    # ticket; the player redeems it via /cache/confirm after ~10s of
                    # actual playback, and only then is the download enqueued.
                    # Movies aren't cached (the cache key is TV-shaped, tmdb/season/
                    # episode); mint_ticket owns that policy and returns None for a
                    # movie, so no ticket is emitted (no extra branch needed here).
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
        # Wait for every scraper, then push the sentinel that ends the drain loop.
        await asyncio.gather(*workers, return_exceptions=True)
        await queue.put(None)

    finisher = asyncio.create_task(_finish())

    count = 0
    try:
        while True:
            stream = await queue.get()
            if stream is None:  # sentinel: all scrapers finished
                break
            count += 1
            # Shape (incl. the cacheTicket-only-when-present rule) lives in
            # core.contracts so it can't drift from the client/crimson-sources.
            yield _ndjson(build_stream_line(stream))
        yield _ndjson(build_done_line(count))
    finally:
        # If the client disconnects mid-stream the generator is closed here —
        # cancel the still-running tasks so they don't leak (no-op if done).
        finisher.cancel()
        for w in workers:
            w.cancel()


async def _enrich_progress_rows(rows: List[Dict]) -> None:
    """Attach per-show "next episode" hints to deduped watch-progress rows so the
    frontend never offers a non-existent or not-yet-aired next episode.

    Each row is one show, carrying its latest watched season+episode. We look up
    that season's TMDB episode list (L1-cached) and add, in place:
      * season_episode_count  — total episodes in the season
      * next_episode_exists   — whether episode_number+1 is a real episode
      * next_episode_air_date — that next episode's air_date (None if n/a/unknown)

    Best-effort and concurrency-bounded; on any per-row failure the row is just
    left unannotated (the frontend then falls back to its old behaviour)."""
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
