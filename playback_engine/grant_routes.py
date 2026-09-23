"""What the client engine cannot do without a server-held secret.

/sign mints a signed crimson-proxy link for a stream the browser resolved, so
PROXY_SECRET never ships to it. /resolve runs a secret-bound source's lookup
here and hands back the raw URL, so only a little control traffic crosses this
backend and the bytes go upstream to edge to viewer. Both are login-gated and
rate-limited, so neither is an anonymous oracle.
"""

import logging
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import resolvers
from core.config import get_settings
from core.private_sources import discover_resolve_grants, load_ref
from core.public_url import public_base_url
from core.rate_limit import limiter
from resolvers import _crimson_proxy
from resolvers.jellyfin import JellyfinResolver, is_configured as jellyfin_is_configured
from scrapers.jellyfin_scraper import JellyfinScraper

from .pipeline import run_single_scraper

logger = logging.getLogger("crimson.grants")

router = APIRouter(tags=["watch"])

_SIGN_MAX_ITEMS = 24


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code}, status_code=status)


async def _json_object(request: Request) -> Optional[Dict]:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _sign_one(item) -> Optional[str]:
    """Only http(s) upstreams are signed; the proxy runs its own SSRF check."""
    if not isinstance(item, dict):
        return None
    url = (item.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    return _crimson_proxy.proxy_url(
        url,
        referer=item.get("referer") or "",
        origin=item.get("origin") or "",
        user_agent=item.get("userAgent") or item.get("user_agent") or "",
    )


@router.post("/sign")
@limiter.limit("240/minute")
async def sign_proxy_links(request: Request):
    """One item or ``{"items": [...]}``, answered with a parallel ``signed`` array
    holding null for anything refused. 503 without a configured proxy, which
    leaves the client on its extension or backend path."""
    if not _crimson_proxy.is_enabled():
        return _error("proxy_unconfigured", 503)
    body = await _json_object(request)
    if body is None:
        return _error("bad_request", 400)
    items = body.get("items")
    if items is None:
        items = [body]
    if not isinstance(items, list) or not items:
        return _error("bad_request", 400)
    return {"ok": True, "signed": [_sign_one(it) for it in items[:_SIGN_MAX_ITEMS]]}


Runner = Callable[..., Awaitable[List[Dict]]]


def _grant_runner(scraper_cls, resolver_cls):
    """Discovery with the scraper, then the resolver's secret-gated
    ``resolve_direct``: one raw-URL stream per quality variant, best first."""

    async def _run(tmdb_id: int, season: int, episode: int, anilist_data: Dict,
                   media_type: str, base_url: str) -> List[Dict]:
        embeds = await run_single_scraper(scraper_cls, tmdb_id, season, episode, anilist_data, media_type)
        resolver = resolver_cls()
        out: List[Dict] = []
        for embed in embeds:
            try:
                streams = await resolver.resolve_direct(embed)
            except Exception as e:
                logger.warning(f"[resolve] {resolver.source_name} resolve_direct failed: {type(e).__name__} - {e}")
                continue
            for res in streams or []:
                if not res.get("url"):
                    continue
                out.append({
                    # The label dedups against the client's own tile for the source.
                    "label": res.get("label") or resolver.source_name,
                    "streamType": res.get("streamType") or "mp4",
                    "url": res["url"],
                    "headers": res.get("headers") or {},
                    "subtitles": [
                        {**s, "url": base_url.rstrip("/") + s["url"]}
                        if base_url and isinstance(s.get("url"), str) and s["url"].startswith("/") else s
                        for s in res.get("subtitles") or []
                    ],
                    "language": res.get("language"),
                })
        return out

    return _run


def _jellyfin_grant_configured() -> bool:
    """Only once the edge holds the Jellyfin host and token
    (``JELLYFIN_EDGE_INJECT``): the edge, not the browser, injects the token."""
    return jellyfin_is_configured() and get_settings().jellyfin_edge_inject


def _build_grants() -> Dict[str, Tuple[Callable[[], bool], Runner]]:
    """source key -> (is_configured, runner). Jellyfin is public; overlay sources
    declare a RESOLVE_GRANT, so this module names none of them."""
    grants: Dict[str, Tuple[Callable[[], bool], Runner]] = {
        "jellyfin": (_jellyfin_grant_configured, _grant_runner(JellyfinScraper, JellyfinResolver)),
    }
    for desc in discover_resolve_grants(resolvers):
        try:
            runner = _grant_runner(load_ref(desc["scraper"]), load_ref(desc["resolver"]))
        except Exception as e:
            logger.warning(f"[resolve] skipping overlay grant {desc.get('keys')}: {type(e).__name__} - {e}")
            continue
        for key in desc["keys"]:
            grants[str(key).lower()] = (desc["is_configured"], runner)
    return grants


_GRANTS = _build_grants()


@router.post("/resolve")
@limiter.limit("120/minute")
async def resolve_grant(request: Request):
    """The body is the client's MediaCtx plus ``source``. 404 for an unknown
    source and 503 for an unconfigured one, both of which keep the client on the
    backend /watch path for it."""
    body = await _json_object(request)
    if body is None:
        return _error("bad_request", 400)
    source = (body.get("source") or "").strip().lower()
    grant = _GRANTS.get(source)
    if not grant:
        return _error("unknown_source", 404)
    is_configured, runner = grant
    if not is_configured():
        return _error("source_unconfigured", 503)

    try:
        tmdb_id = int(body.get("tmdbId") or body.get("tmdb_id"))
    except (TypeError, ValueError):
        return _error("bad_request", 400)
    media_type = "movie" if (body.get("mediaType") or "tv") == "movie" else "tv"
    try:
        season, episode = int(body.get("season") or 1), int(body.get("episode") or 1)
    except (TypeError, ValueError):
        season, episode = 1, 1
    anilist_data = {
        "title": body.get("title"),
        "title_english": body.get("titleEnglish"),
        "title_romaji": body.get("titleRomaji"),
        "title_native": body.get("titleNative"),
        "synonyms": body.get("synonyms") or [],
    }

    try:
        streams = await runner(tmdb_id, season, episode, anilist_data, media_type, public_base_url(request))
    except Exception as e:
        logger.error(f"[resolve] grant for {source!r} failed: {type(e).__name__} - {e}")
        return _error("resolve_failed", 502)
    return {"ok": True, "streams": streams}
