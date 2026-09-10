"""Playback: the NDJSON /watch streams, the client-offload grants, the movie-web
bridge, and the cache and telemetry beacons.

The /watch routes emit the progressive NDJSON the player consumes, one line per
source. The /sign and /resolve grants hand the client engine the few crumbs it
cannot derive without a server-held secret (New System 8a). The /mw bridge
reshapes the same pipeline into movie-web's native Stream JSON. The shared
scrape/resolve pipeline lives in ``web.pipeline``.
"""

import logging
import os
import re
import json
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import resolvers as _resolvers_pkg
from core.rate_limit import limiter
from core.http_client import http_client
from core.private_sources import discover_resolve_grants, load_ref
from resolvers import _crimson_proxy
from resolvers.jellyfin import JellyfinResolver, is_configured as jellyfin_is_configured
from scrapers.jellyfin_scraper import JellyfinScraper
from cache_engine.downloader import manager as cache_manager
from metadata_engine.tmdb import fetch_tmdb_show, fetch_tmdb_movie

from web.context import telemetry_store
from web.pipeline import run_single_scraper, stream_watch_response
from web.queries import (
    get_anilist_id,
    get_extra_movie_id,
    get_movie_info,
    get_show_info,
    get_tmdb_season,
)
from web.util import _STREAM_HEADERS, _public_base_url

logger = logging.getLogger("crimson.watch")

router = APIRouter()


@router.get("/watch/{tmdb_id}/{season_number}/{episode_number}")
@limiter.limit("30/minute")
async def get_watch_links(request: Request, tmdb_id: int, season_number: int, episode_number: int):
    """Streaming links as progressive NDJSON, one line per source, emitted as soon
    as that source resolves. Works for TMDB seasons with no AniList mapping too,
    since the proxy sources play off the TMDB id."""
    anilist_id = get_anilist_id(tmdb_id, season_number)

    fallback_title = None
    if not anilist_id:
        info = get_show_info(tmdb_id)
        fallback_title = info.get("title") if info else None
        if not fallback_title:
            async with http_client() as client:
                show = await fetch_tmdb_show(client, tmdb_id)
            fallback_title = show.get("title")

    return StreamingResponse(
        stream_watch_response(tmdb_id, season_number, episode_number, anilist_id,
                              fallback_title, base_url=_public_base_url(request)),
        media_type="application/x-ndjson",
        headers=_STREAM_HEADERS,
    )


@router.get("/watch/movie/{tmdb_id}")
@limiter.limit("30/minute")
async def get_movie_watch_links(request: Request, tmdb_id: int):
    """Streaming links for a standalone movie, as the same progressive NDJSON the
    TV route emits.

    A movie has no season, episode or AniList mapping, so only the movie-capable
    sources run and the meta line carries nulls the player ignores. Declared before
    /watch/{anilist_id}/{episode_number} so the literal 'movie' segment matches
    here rather than failing that route's int parse."""
    # A title helps any title-keyed source. The stored row first, then a live
    # fetch, and never a hard failure since sources can still play off the id.
    info = get_movie_info(tmdb_id)
    fallback_title = info.get("title") if info else None
    if not fallback_title:
        try:
            async with http_client() as client:
                movie = await fetch_tmdb_movie(client, tmdb_id)
            fallback_title = movie.get("title")
        except Exception as e:
            logger.warning(f"movie title fetch failed for {tmdb_id}: {e}")

    return StreamingResponse(
        stream_watch_response(tmdb_id, None, None, None,
                              fallback_title, base_url=_public_base_url(request),
                              media_type="movie"),
        media_type="application/x-ndjson",
        headers=_STREAM_HEADERS,
    )


# --- crimson-proxy sign grant (New System 8a) ------------------------------
# On the web-only E2 path the client resolves a stream in the browser and needs a
# signed proxy link to relay the bytes off the backend, but PROXY_SECRET must
# never ship to the browser. So the client sends the upstream URL and the headers
# the CDN wants, and gets back the signed link. That is what keeps the secret
# server-side while letting the client drive what gets fetched.
#
# Login-gated and rate-limited, so it cannot be an anonymous signing oracle. Only
# http(s) upstreams are signed, and the proxy still runs its own SSRF check.
_SIGN_MAX_ITEMS = 24


def _sign_one(item: Dict) -> Optional[str]:
    """Sign one item into a proxy link, or None if its url is missing or not
    http(s)."""
    if not isinstance(item, dict):
        return None
    url = (item.get("url") or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    return _crimson_proxy.proxy_url(
        url,
        referer=(item.get("referer") or ""),
        origin=(item.get("origin") or ""),
        user_agent=(item.get("userAgent") or item.get("user_agent") or ""),
    )


@router.post("/sign")
@limiter.limit("240/minute")
async def sign_proxy_links(request: Request):
    """Mint signed proxy links for client-resolved streams.

    Accepts one item or ``{"items": [...]}`` and always returns a parallel
    ``signed`` array, with null for anything refused.

    503 when the external proxy is unconfigured, which leaves the client on its
    extension or backend path, so an unconfigured proxy never breaks playback."""
    if not _crimson_proxy.is_enabled():
        return JSONResponse({"ok": False, "error": "proxy_unconfigured"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    items = body.get("items")
    if items is None:
        items = [body]  # single-object form
    if not isinstance(items, list) or not items:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if len(items) > _SIGN_MAX_ITEMS:
        items = items[:_SIGN_MAX_ITEMS]

    signed = [_sign_one(it) for it in items]
    return {"ok": True, "signed": signed}


# --- client-side resolve grants ---------------------------------------------
# Some operator-owned sources cannot run wholly in the browser because the final
# hop needs a server-held secret, such as the Jellyfin token. But only the resolve
# needs it: the URL it yields is a stream the viewer or the proxy edge can fetch.
# So /resolve does the lookup server-side and returns the raw URL plus the headers
# the upstream wants, and the client engine delivers the bytes. The heavy media
# never travels through this backend, only a little control traffic.


def _make_offload_grant_runner(scraper_ref: str, resolver_ref: str):
    """Build a /resolve runner for a source declared by an overlay's RESOLVE_GRANT.

    The runner runs discovery, unlocks each embed with the resolver's secret-gated
    ``resolve_direct``, and returns one raw-URL stream per quality variant, leaving
    byte delivery to the client engine. The descriptor's class refs are strings, to
    dodge the scraper/resolver import cycle, and are resolved once here at import."""
    scraper_cls = load_ref(scraper_ref)
    resolver_cls = load_ref(resolver_ref)

    async def _runner(
        tmdb_id: int, season_num: int, episode_num: int,
        anilist_data: Dict, media_type: str, base_url: str,
    ) -> List[Dict]:
        embeds = await run_single_scraper(
            scraper_cls, tmdb_id, season_num, episode_num, anilist_data, media_type
        )
        if not embeds:
            return []
        resolver = resolver_cls()
        out: List[Dict] = []
        for embed in embeds:
            try:
                streams = await resolver.resolve_direct(embed)
            except Exception as e:
                logger.warning(f"[resolve] {resolver.source_name} resolve_direct failed: "
                               f"{type(e).__name__} - {e}")
                continue
            # One stream per quality variant, best first.
            for res in streams or []:
                if not res.get("url"):
                    continue
                subs = res.get("subtitles") or []
                if base_url:
                    subs = [
                        {**s, "url": base_url.rstrip("/") + s["url"]}
                        if isinstance(s.get("url"), str) and s["url"].startswith("/") else s
                        for s in subs
                    ]
                out.append({
                    # A per-quality label, which dedups with the client tile
                    "label": res.get("label") or resolver.source_name,
                    "streamType": res.get("streamType") or "mp4",
                    "url": res["url"],
                    "headers": res.get("headers") or {},
                    "subtitles": subs,
                    "language": res.get("language"),
                })
        return out

    return _runner


def _jellyfin_edge_inject_enabled() -> bool:
    """Opt-in switch for delivering Jellyfin off-backend through edge token
    injection. Off by default, leaving Jellyfin on the backend /watch proxy. Turn
    it on only once the proxy is deployed with its Jellyfin host and token, since
    the edge rather than the browser holds that token."""
    return (os.getenv("JELLYFIN_EDGE_INJECT", "").strip().lower() in ("1", "true", "yes", "on"))


def _jellyfin_grant_configured() -> bool:
    return jellyfin_is_configured() and _jellyfin_edge_inject_enabled()


async def _grant_jellyfin(
    tmdb_id: int, season_num: int, episode_num: int,
    anilist_data: Dict, media_type: str, base_url: str,
) -> List[Dict]:
    """Resolve the Jellyfin item to its raw, token-less absolute URL.

    The client delivers it through the proxy, which injects the access token at the
    edge, so the bytes go Jellyfin to edge to viewer and the token never reaches
    the browser. ``base_url`` is unused, as there is no same-origin path here."""
    embeds = await run_single_scraper(
        JellyfinScraper, tmdb_id, season_num, episode_num, anilist_data, media_type
    )
    if not embeds:
        return []
    resolver = JellyfinResolver()
    out: List[Dict] = []
    for embed in embeds:
        try:
            res = await resolver.resolve_direct(embed)
        except Exception as e:
            logger.warning(f"[resolve] jellyfin resolve_direct failed: {type(e).__name__} - {e}")
            continue
        if not res or not res.get("url"):
            continue
        out.append({
            "label": resolver.source_name,  # dedups with the /watch tile
            "streamType": res.get("streamType") or "hls",
            "url": res["url"],
            # None needed: the edge supplies the token and Authorization itself.
            "headers": {},
            "subtitles": [],
            "language": None,
        })
    return out


# source key -> (is_configured probe, runner). Jellyfin is operator-owned and
# always public; any other secret-bound source comes from the overlay's
# RESOLVE_GRANT descriptors. A base build discovers none, so only Jellyfin is
# wired and /resolve 404s for anything else, leaving the client on its backend
# path. That keeps this file free of any overlay source name while giving each
# injected one a client-delivery path for free.
def _build_resolve_grants() -> Dict:
    grants = {"jellyfin": (_jellyfin_grant_configured, _grant_jellyfin)}
    for desc in discover_resolve_grants(_resolvers_pkg):
        try:
            runner = _make_offload_grant_runner(desc["scraper"], desc["resolver"])
        except Exception as e:  # a broken overlay descriptor must not sink /resolve
            logger.warning(f"[resolve] skipping overlay grant {desc.get('keys')}: "
                           f"{type(e).__name__} - {e}")
            continue
        for key in desc["keys"]:
            grants[str(key).lower()] = (desc["is_configured"], runner)
    return grants


_RESOLVE_GRANTS = _build_resolve_grants()


@router.post("/resolve")
@limiter.limit("120/minute")
async def resolve_grant(request: Request):
    """Server-side resolve grant for secret-bound sources.

    The body is the client's MediaCtx plus a ``source`` key. Returns
    ``{ok, streams:[...]}`` carrying raw CDN URLs, leaving byte delivery to the
    client engine.

    503 when that source is unconfigured, which keeps the client on the backend
    /watch line for it."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    source = (body.get("source") or "").strip().lower()
    grant = _RESOLVE_GRANTS.get(source)
    if not grant:
        return JSONResponse({"ok": False, "error": "unknown_source"}, status_code=404)
    is_conf, runner = grant
    if not is_conf():
        return JSONResponse({"ok": False, "error": "source_unconfigured"}, status_code=503)

    try:
        tmdb_id = int(body.get("tmdbId") or body.get("tmdb_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    media_type = "movie" if (body.get("mediaType") or "tv") == "movie" else "tv"
    try:
        season_num = int(body.get("season") or 1)
        episode_num = int(body.get("episode") or 1)
    except (TypeError, ValueError):
        season_num, episode_num = 1, 1

    # The same fields the client already carries, enriched via /scrape-meta. The
    # scraper's candidate-title builder skips None values.
    anilist_data = {
        "title": body.get("title"),
        "title_english": body.get("titleEnglish"),
        "title_romaji": body.get("titleRomaji"),
        "title_native": body.get("titleNative"),
        "synonyms": body.get("synonyms") or [],
    }

    base_url = _public_base_url(request)
    try:
        streams = await runner(
            tmdb_id, season_num, episode_num, anilist_data, media_type, base_url
        )
    except Exception as e:
        logger.error(f"[resolve] grant for {source!r} failed: {type(e).__name__} - {e}")
        return JSONResponse({"ok": False, "error": "resolve_failed"}, status_code=502)

    return {"ok": True, "streams": streams}


# --- movie-web bridge (/mw) -------------------------------------------------
# Reshapes the existing pipeline into @movie-web/providers' native `Stream` JSON,
# so a modified movie-web fork can consume Crimson as a single source instead of
# scraping locally. These are the only routes an API key can reach: a valid
# X-API-Key unlocks /mw and nothing else.
#
# Two differences from the frontend /watch routes:
#   * the output is one buffered JSON document, not progressive NDJSON, because
#     movie-web's runner wants a source to return its streams as a value
#   * iframe-type sources are dropped, since movie-web has no iframe player. The
#     direct sources carry through unchanged.
def _mw_slug(text: Optional[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "src"


def _mw_captions(subtitles: Optional[List[Dict]]) -> List[Dict]:
    """Map Crimson's subtitle tracks onto movie-web's `Caption` shape.

    The URLs are already absolutized same-origin proxy paths serving WebVTT, so
    the type defaults to vtt, honouring an explicit .srt extension when present."""
    out: List[Dict] = []
    for i, s in enumerate(subtitles or []):
        url = s.get("url")
        if not url:
            continue
        label = s.get("label") or s.get("lang") or "Unknown"
        ctype = "srt" if ".srt" in url.lower() else "vtt"
        out.append({
            "id": f"{_mw_slug(label)}-{i}",
            "type": ctype,
            "url": url,
            "language": s.get("lang") or label,
            "hasCorsRestrictions": False,
        })
    return out


def _to_mw_stream(line: Dict, idx: int) -> Optional[Dict]:
    """One NDJSON `stream` line as a movie-web `Stream`, or None when movie-web
    cannot play it."""
    stype = line.get("streamType")
    url = line.get("url")
    if not url or stype == "iframe":
        return None
    captions = _mw_captions(line.get("subtitles"))
    # Empty on purpose: advertising no playback guarantees makes the fork route
    # the stream through its own proxy, which is also where it injects the key.
    base = {
        "id": f"crimson-{_mw_slug(line.get('source'))}-{idx}",
        "flags": [],
        "captions": captions,
        # Hints the fork can surface. movie-web ignores unknown keys.
        "crimsonSource": line.get("source"),
        "crimsonLanguage": line.get("language"),
    }
    if stype == "hls":
        return {**base, "type": "hls", "playlist": url}
    # movie-web's `file` shape keys streams by quality, and Crimson does not probe
    # it, so everything goes on the single "unknown" rung.
    return {**base, "type": "file", "qualities": {"unknown": {"type": "mp4", "url": url}}}


async def _collect_mw_streams(agen) -> Tuple[Optional[Dict], List[Dict]]:
    """Drain the NDJSON watch generator into (meta, movie-web streams). Reuses the
    whole real pipeline and only reshapes its output."""
    meta: Optional[Dict] = None
    streams: List[Dict] = []
    idx = 0
    async for raw in agen:
        try:
            evt = json.loads(raw)
        except Exception:
            continue
        etype = evt.get("type")
        if etype == "meta":
            meta = evt
        elif etype == "stream":
            mw = _to_mw_stream(evt, idx)
            idx += 1
            if mw:
                streams.append(mw)
        elif etype == "unaired":
            meta = {**(meta or {}), "unaired": True, "air_date": evt.get("air_date")}
    return meta, streams


@router.get("/mw/watch/movie/{tmdb_id}")
@limiter.limit("30/minute")
async def mw_watch_movie(request: Request, tmdb_id: int):
    """Bridge streams for a standalone movie, as one JSON document of native
    movie-web `Stream`s. Declared before the TV route so the literal 'movie'
    segment matches here. Requires a valid X-API-Key or a session."""
    info = get_movie_info(tmdb_id)
    fallback_title = info.get("title") if info else None
    if not fallback_title:
        try:
            async with http_client() as client:
                movie = await fetch_tmdb_movie(client, tmdb_id)
            fallback_title = movie.get("title")
        except Exception as e:
            logger.warning(f"[mw] movie title fetch failed for {tmdb_id}: {e}")

    meta, streams = await _collect_mw_streams(
        stream_watch_response(tmdb_id, None, None, None, fallback_title,
                              base_url=_public_base_url(request), media_type="movie")
    )
    return {
        "success": True,
        "media": "movie",
        "tmdb_id": tmdb_id,
        "title": (meta or {}).get("title") or fallback_title,
        "streams": streams,
    }


@router.get("/mw/watch/{tmdb_id}/{season_number}/{episode_number}")
@limiter.limit("30/minute")
async def mw_watch_tv(request: Request, tmdb_id: int, season_number: int, episode_number: int):
    """Bridge streams for a TV episode, as one JSON document of native movie-web
    `Stream`s. Mirrors the frontend /watch route's id and title resolution, then
    reshapes the output. Requires a valid X-API-Key or a session."""
    anilist_id = get_anilist_id(tmdb_id, season_number)
    fallback_title = None
    if not anilist_id:
        info = get_show_info(tmdb_id)
        fallback_title = info.get("title") if info else None
        if not fallback_title:
            async with http_client() as client:
                show = await fetch_tmdb_show(client, tmdb_id)
            fallback_title = show.get("title")

    meta, streams = await _collect_mw_streams(
        stream_watch_response(tmdb_id, season_number, episode_number, anilist_id,
                              fallback_title, base_url=_public_base_url(request))
    )
    payload = {
        "success": True,
        "media": "tv",
        "tmdb_id": tmdb_id,
        "season": season_number,
        "episode": episode_number,
        "title": (meta or {}).get("title") or fallback_title,
        "streams": streams,
    }
    if meta and meta.get("unaired"):
        payload["unaired"] = True
        payload["air_date"] = meta.get("air_date")
    return payload


@router.post("/cache/confirm")
@limiter.limit("120/minute")
async def confirm_cache(request: Request):
    """Redeem a ``cacheTicket`` once the viewer has watched that source for a few
    seconds, which is when the stream is enqueued for caching. That way the cached
    source is the one the viewer chose, not whichever resolved fastest.

    The ticket is HMAC-signed by /watch, so no arbitrary URL reaches the
    downloader. Always 200, so it never leaks whether caching is on or whether the
    episode was already cached."""
    try:
        body = await request.json()
        ticket = (body or {}).get("ticket") or ""
    except Exception:
        ticket = ""
    accepted = await cache_manager.confirm_ticket(ticket) if ticket else False
    return {"ok": bool(accepted)}


@router.post("/telemetry/resolve")
@limiter.limit("60/minute")
async def telemetry_resolve(request: Request):
    """Ingest an anonymous per-source resolve beacon from the client engine.

    Strictly aggregate: no title, user or IP is stored. Restores the source-success
    visibility lost when resolving moved client-side. Always 200, so a beacon can
    be fire-and-forget."""
    try:
        body = await request.json()
        events = (body or {}).get("events") or []
    except Exception:
        events = []
    rows = 0
    if isinstance(events, list) and events:
        try:
            rows = await run_in_threadpool(telemetry_store.record_batch, events)
        except Exception as e:
            logger.warning(f"telemetry ingest failed: {e}")
    return {"ok": True, "recorded": rows}


@router.get("/watch/{anilist_id}/{episode_number}")
@limiter.limit("30/minute")
async def deprecated_watch(request: Request, anilist_id: int, episode_number: int, season_part: int = Query(1)):
    """Watch by anilist_id. TV seasons map to the canonical /watch route, while
    extras have no TMDB season number and are served directly here.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id, season_number = mapping

    # An extra that is a film in TMDB's own right has a movie page, not an episode
    # page, so it goes through the movie pipeline off its own id. The
    # movie-capable sources can then find it, and its cache key lands in the movie
    # namespace instead of colliding with an episode of the parent show.
    if season_number is None:
        movie_id = get_extra_movie_id(anilist_id)
        if movie_id:
            info = get_movie_info(movie_id)
            return StreamingResponse(
                stream_watch_response(movie_id, None, None, None,
                                      info.get("title") if info else None,
                                      base_url=_public_base_url(request),
                                      media_type="movie"),
                media_type="application/x-ndjson",
                headers=_STREAM_HEADERS,
            )

    # Served directly rather than 301-redirecting to the canonical 3-segment
    # route, because a redirect is fatal on WebKit: it drops the Authorization
    # header when fetch() follows, so the request hits the login wall
    # unauthenticated, the client clears the session and the user is bounced out.
    #
    # A remaining extra has no numbered season, so it plays as season 0, the
    # specials season TMDB and the streaming sites both use. It used to borrow
    # season 1, which pointed every special at the first episode of the show
    # proper and minted its cache ticket under that episode's key.
    return StreamingResponse(
        stream_watch_response(tmdb_id, season_number if season_number is not None else 0,
                              episode_number, anilist_id,
                              base_url=_public_base_url(request)),
        media_type="application/x-ndjson",
        headers=_STREAM_HEADERS,
    )
