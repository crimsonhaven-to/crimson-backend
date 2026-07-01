"""Playback endpoints: the NDJSON /watch streams, the client-offload grants, the
movie-web bridge, and the cache/telemetry beacons.

The /watch routes emit the progressive NDJSON the player consumes (one line per
source). The /sign + /resolve grants hand the client engine the few crumbs it
can't derive without a server-held secret (New System §8a). The /mw bridge
reshapes the same pipeline into movie-web's native Stream JSON. Lifted verbatim
from ``api.py``; the shared scrape/resolve pipeline lives in ``web.pipeline``.
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

from core.rate_limit import limiter
from core.http_client import http_client
from resolvers import _crimson_proxy
from resolvers.jellyfin import JellyfinResolver, is_configured as jellyfin_is_configured
from resolvers.febbox import FebboxResolver, is_configured as febbox_is_configured
from scrapers.jellyfin_scraper import JellyfinScraper
from scrapers.showbox_scraper import ShowBoxScraper
from cache_engine.downloader import manager as cache_manager
from metadata_engine.tmdb import fetch_tmdb_show, fetch_tmdb_movie

from web.context import telemetry_store
from web.pipeline import run_single_scraper, stream_watch_response
from web.queries import get_anilist_id, get_movie_info, get_show_info, get_tmdb_season
from web.util import _STREAM_HEADERS, _public_base_url

logger = logging.getLogger("crimson.watch")

router = APIRouter()


@router.get("/watch/{tmdb_id}/{season_number}/{episode_number}")
@limiter.limit("30/minute")
async def get_watch_links(request: Request, tmdb_id: int, season_number: int, episode_number: int):
    """Get streaming links as a progressive NDJSON stream (one line per source,
    emitted as soon as that source resolves). Works even for TMDB seasons with no
    AniList mapping (long shows like Naruto) — the proxy sources play off the TMDB id."""
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
    """Streaming links for a standalone MOVIE (TMDB *movie* id), as the same
    progressive NDJSON the TV watch route emits — one line per source. Movies have
    no season/episode and no AniList mapping; only the movie-capable TMDB-keyed
    sources run. Declared before /watch/{anilist_id}/{episode_number} so the literal
    'movie' segment is matched here rather than failing that route's int parse.

    The meta line carries null season_number/episode_number; the player ignores
    them for movies."""
    # A title helps the title-based movie source (ShowBox). Prefer the stored row,
    # then a live TMDB fetch; never hard-fail (sources can still play off the id).
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


# --- crimson-proxy sign grant (New System §8a) -----------------------------
# The E2 (web-only, no-extension) path: the client resolves a stream in the
# browser and needs a *signed* crimson-proxy link to relay the segment bytes off
# the backend — but PROXY_SECRET must never ship to the browser. So the client
# sends the upstream URL(s) + the headers the CDN wants injected here, and we hand
# back the signed proxy link(s). This is the only thing that keeps PROXY_SECRET
# server-side while letting the client drive what gets fetched.
#
# Login-gated (NOT in _PUBLIC_PREFIXES) + rate-limited, so it can't be used as an
# anonymous free signing/relay oracle. We only sign http(s) upstreams; the proxy
# itself still runs its own isSafeUpstream SSRF check before fetching.
_SIGN_MAX_ITEMS = 24


def _sign_one(item: Dict) -> Optional[str]:
    """Sign a single ``{url, referer?, origin?, userAgent?}`` into a proxy link, or
    None if the url is missing / not http(s)."""
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
    """Mint signed crimson-proxy link(s) for client-resolved streams (New System
    §8a). Accepts either a single ``{url, referer, origin, userAgent}`` object or
    ``{"items": [ … ]}`` for batch signing, and always returns a parallel
    ``signed`` array (null for any item we refuse to sign).

    Returns 503 when the external proxy isn't configured (no ``CRIMSON_PROXY_BASE``
    / ``PROXY_SECRET``) — the client then stays on E3 (extension) or E0 (backend),
    exactly as today, so an unconfigured proxy never breaks playback."""
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


# --- client-side resolve grants (New System: take the backend out of the byte path) ---
# Some operator-owned sources can't run wholly in the viewer's browser because the
# final hop needs a server-held secret — e.g. the Jellyfin access token. But only
# the *resolve* needs the secret; the URL it yields is a stream the viewer (or the
# crimson-proxy edge) can fetch. So /resolve does the token lookup server-side and
# returns the **raw** stream URL + the headers the upstream wants — and the client
# engine delivers the bytes (extension E3 / signed crimson-proxy E2). The heavy
# mp4/HLS never travels through this backend; only a little control traffic does.


async def _grant_febbox(
    tmdb_id: int, season_num: int, episode_num: int,
    anilist_data: Dict, media_type: str, base_url: str,
) -> List[Dict]:
    """
    :3
    """
    embeds = await run_single_scraper(
        ShowBoxScraper, tmdb_id, season_num, episode_num, anilist_data, media_type
    )
    if not embeds:
        return []
    resolver = FebboxResolver()
    out: List[Dict] = []
    for embed in embeds:
        try:
            streams = await resolver.resolve_direct(embed)
        except Exception as e:
            logger.warning(f"[resolve] febbox resolve_direct failed: {type(e).__name__} - {e}")
            continue
        # resolve_direct returns one stream per quality variant (best-first).
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
                # per-quality "ShowBox (1080p)" -> dedups with the client tile
                "label": res.get("label") or resolver.source_name,
                "streamType": res.get("streamType") or "mp4",
                "url": res["url"],
                "headers": res.get("headers") or {},
                "subtitles": subs,
                "language": res.get("language"),
            })
    return out


def _jellyfin_edge_inject_enabled() -> bool:
    """Opt-in switch for delivering Jellyfin off-backend via crimson-proxy edge
    token injection. OFF by default → Jellyfin stays fully on the backend /watch
    proxy (today's behaviour, no regression). Flip it on ONLY after the proxy is
    deployed with NITRO_JELLYFIN_HOSTS + NITRO_JELLYFIN_TOKEN, since the edge — not
    the browser — holds the token and the client path is E2-only."""
    return (os.getenv("JELLYFIN_EDGE_INJECT", "").strip().lower() in ("1", "true", "yes", "on"))


def _jellyfin_grant_configured() -> bool:
    return jellyfin_is_configured() and _jellyfin_edge_inject_enabled()


async def _grant_jellyfin(
    tmdb_id: int, season_num: int, episode_num: int,
    anilist_data: Dict, media_type: str, base_url: str,
) -> List[Dict]:
    """Resolve the Jellyfin item to its RAW, token-less absolute URL. The client
    delivers it E2-only through the crimson-proxy, which injects the access token at
    the edge — so the heavy bytes go Jellyfin → edge → viewer and the token never
    reaches the browser. ``base_url`` is unused (no same-origin proxy path here)."""
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
            "label": resolver.source_name,  # "Jellyfin" -> dedups with the /watch tile
            "streamType": res.get("streamType") or "hls",
            "url": res["url"],
            # No upstream headers: the edge supplies the token + Authorization itself.
            "headers": {},
            "subtitles": [],
            "language": None,
        })
    return out


# Per-source grant registry: source key -> (is_configured probe, runner). Add an
# operator-owned secret source here and it gains a client-delivery path for free.
_RESOLVE_GRANTS = {
    "jellyfin": (_jellyfin_grant_configured, _grant_jellyfin),
    "febbox": (febbox_is_configured, _grant_febbox),
    "showbox": (febbox_is_configured, _grant_febbox),
}


@router.post("/resolve")
@limiter.limit("120/minute")
async def resolve_grant(request: Request):
    """Server-side resolve grant for cookie/secret-bound sources (New System).

    Body is the client's MediaCtx + a ``source`` key:
    ``{source, tmdbId, mediaType, season, episode, title, titleEnglish,
    titleRomaji, titleNative, synonyms}``. Returns
    ``{ok, streams:[{label, streamType, url, headers, subtitles, language}]}`` with
    **raw** CDN URLs — the client engine handles the actual byte delivery.

    503 when the requested source isn't configured;
    the client then keeps using the backend /watch line for it."""
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

    # The title bundle the discovery scraper matches on — the same fields the client
    # already carries (and enriched via /scrape-meta). None values are simply skipped
    # by the scraper's candidate-title builder.
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
# A thin compatibility surface that re-shapes the existing scrape+resolve
# pipeline into @movie-web/providers' native `Stream` JSON, so a modified
# movie-web fork can consume Crimson as a single "source" instead of scraping
# locally. These routes are the ONLY ones an API key can reach (see the login
# wall): a valid X-API-Key unlocks /mw and nothing else.
#
# Two differences from the frontend /watch routes:
#   * the output is one buffered JSON document (a streams[] array), not the
#     progressive NDJSON our own player consumes — movie-web's runner wants a
#     source to return its streams as a value;
#   * `iframe`-type sources (Movish player-proxy, AnimeSuge /player) are dropped:
#     movie-web has no iframe player, only direct hls/file playback. The direct
#     sources (PlayIMDb, Cinema.bz, ShowBox, VidSrc, Jellyfin, Cache, …) carry
#     through unchanged.
def _mw_slug(text: Optional[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "src"


def _mw_captions(subtitles: Optional[List[Dict]]) -> List[Dict]:
    """Map Crimson's `{label, lang, url}` subtitle tracks onto movie-web's
    `Caption` shape. URLs are already absolutized same-origin proxy paths (see
    resolve_streams), which serve WebVTT — so default the type to vtt, honoring
    an explicit .srt extension when present."""
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
    """One NDJSON `stream` line -> one movie-web `Stream`, or None if movie-web
    can't play it (iframe sources, or a line with no URL)."""
    stype = line.get("streamType")
    url = line.get("url")
    if not url or stype == "iframe":
        return None
    captions = _mw_captions(line.get("subtitles"))
    # `flags` is intentionally empty: it advertises no special playback
    # guarantees, so the fork routes the stream through its own proxy (which is
    # also where it injects the bridge key) rather than fetching us directly.
    base = {
        "id": f"crimson-{_mw_slug(line.get('source'))}-{idx}",
        "flags": [],
        "captions": captions,
        # Non-standard hints the fork can surface (source label + dub/sub
        # language). movie-web ignores unknown keys, so this is additive.
        "crimsonSource": line.get("source"),
        "crimsonLanguage": line.get("language"),
    }
    if stype == "hls":
        return {**base, "type": "hls", "playlist": url}
    # mp4 / any direct file: movie-web's `file` shape keys streams by quality.
    # Crimson doesn't probe quality, so expose it as the single "unknown" rung.
    return {**base, "type": "file", "qualities": {"unknown": {"type": "mp4", "url": url}}}


async def _collect_mw_streams(agen) -> Tuple[Optional[Dict], List[Dict]]:
    """Drain the NDJSON watch generator into (meta, movie-web streams[]). Reuses
    the entire real pipeline (scrape, resolve, dedup, air-date + localized-title
    handling) — this only reshapes the output, it does not re-implement it."""
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
    """movie-web bridge — streams for a standalone MOVIE (TMDB movie id), as a
    single JSON document of native movie-web `Stream`s. Declared before the TV
    route so the literal 'movie' segment matches here. Requires a valid
    X-API-Key (or an admin/user session)."""
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
    """movie-web bridge — streams for a TV episode (TMDB show id + season +
    episode), as a single JSON document of native movie-web `Stream`s. Mirrors
    the frontend /watch route's id/title resolution, then reshapes the output.
    Requires a valid X-API-Key (or an admin/user session)."""
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
    """Player calls this once the viewer has actually watched a source for a few
    seconds, passing back the ``cacheTicket`` that source carried. Only then is
    that exact stream enqueued for server-side caching — so we cache the source
    the viewer *chose* (its quality + language), not whichever resolved fastest.

    The ticket is HMAC-signed by ``/watch``, so no arbitrary URL can be injected
    into the downloader. Behind the login wall; always 200 so it never leaks
    whether caching is on or whether the episode was already cached."""
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

    Body: ``{"events": [{"source": "Cinema.bz (tcloud)", "ok": true, "env": "extension"}, …]}``.
    Strictly aggregate + anonymous — no title, no user, no IP is stored (see
    telemetry_engine). Restores the source-success visibility lost when resolving
    moved client-side. Behind the login wall + rate-limited; always 200 so a
    beacon can be fire-and-forget."""
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
    """
    Watch by anilist_id. TV seasons redirect to the canonical /watch route;
    extras (specials/OVAs/movies) have no TMDB season number, so they are served
    directly here.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id, season_number = mapping
    # Serve the stream directly rather than 301-redirecting to the canonical
    # 3-segment route. A redirect is fatal on WebKit (all iOS browsers + Safari):
    # it drops the Authorization header when fetch() follows the redirect, so the
    # redirected request hits the login wall unauthenticated → 401 → the client
    # clears the session and the user is bounced to the login wall. Extras
    # (special/OVA/movie) have no numbered season — use season 1 for URL builders.
    return StreamingResponse(
        stream_watch_response(tmdb_id, season_number if season_number is not None else 1,
                              episode_number, anilist_id,
                              base_url=_public_base_url(request)),
        media_type="application/x-ndjson",
        headers=_STREAM_HEADERS,
    )
