"""Same-origin stream relays and the backend-hosted player page.

A relay exists where the upstream gates on a token, Referer, UA or ASN the
viewer's browser cannot present, or serves no usable CORS. They are loaded by
<video> and hls.js, which cannot carry the bearer, so each one is either signed
or token-injecting and is exempt from the login wall.

Overlay modules that ship a ``proxy_fetch`` get a relay at ``/<module>_proxy``,
wired by the shape of that function's signature, so this module names no
overlay source.
"""

import inspect
import logging

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

import resolvers
from core.player import is_safe_src, render_player
from core.private_sources import overlay_modules
from core.proxy_response import proxy_response
from resolvers.jellyfin import proxy_fetch as jellyfin_proxy_fetch

logger = logging.getLogger("crimson.proxies")

router = APIRouter(tags=["watch"])

# Base-build resolvers whose relays are declared on this router directly.
_WIRED = {"jellyfin", "local", "cache"}


async def _relay(fetch, **kwargs):
    """ValueError is a refused signature or host, so 403; a network failure 502."""
    try:
        return await fetch(**kwargs)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")


@router.api_route("/jellyfin_proxy/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def jellyfin_proxy(request: Request, path: str):
    """Injects the Jellyfin token server-side, so it never reaches the browser,
    and rewrites playlists to flow back through here."""
    result = await _relay(
        jellyfin_proxy_fetch,
        path=path,
        query_string=request.url.query,
        method=request.method,
        body=await request.body() if request.method == "POST" else None,
        range_header=request.headers.get("range"),
    )
    return proxy_response(*result, forward_bytes_headers=True)


@router.get("/player")
async def player(
    src: str = Query(..., description="Same-origin stream path to play"),
    stream_type: str = Query("", alias="type", description="hls or mp4 (inferred if omitted)"),
    title: str = Query("", description="Optional title"),
):
    """A themed player page for a same-origin stream, which the client iframes.
    ``src`` must be a same-origin path, so it cannot embed external content."""
    if not is_safe_src(src):
        raise HTTPException(status_code=400, detail="Invalid src (must be a same-origin path)")
    return Response(
        content=render_player(src=src, stream_type=stream_type, title=title),
        media_type="text/html; charset=utf-8",
    )


# --- overlay relays ---------------------------------------------------------------
def _signed_stream(fetch):
    async def _route(request: Request):
        q = request.query_params
        return proxy_response(*await _relay(
            fetch, url=q.get("u"), sig=q.get("s"), range_header=request.headers.get("range"),
        ))
    return _route


def _signed_stream_with_headers(fetch):
    async def _route(request: Request):
        q = request.query_params
        return proxy_response(*await _relay(
            fetch, url=q.get("u"), origin=q.get("o"), referer=q.get("r"), sig=q.get("s"),
            range_header=request.headers.get("range"),
        ))
    return _route


def _reverse_proxy(fetch):
    async def _route(request: Request, host: str, path: str):
        return proxy_response(*await _relay(
            fetch, host=host, path=path, query_string=request.url.query, method=request.method,
            body=await request.body() if request.method == "POST" else None,
            range_header=request.headers.get("range"),
        ))
    return _route


def register_overlay_proxies(app: FastAPI) -> tuple:
    """Mount each overlay relay on ``app`` and return their path prefixes, which
    the login wall must let through."""
    prefixes = []
    for module in overlay_modules(resolvers, skip=_WIRED):
        fetch = getattr(module, "proxy_fetch", None)
        if fetch is None:
            continue
        params = set(inspect.signature(fetch).parameters)
        if {"host", "path"} <= params:
            route, suffix, methods = _reverse_proxy(fetch), "/h/{host}/{path:path}", ["GET", "POST"]
        elif {"origin", "referer"} <= params:
            route, suffix, methods = _signed_stream_with_headers(fetch), "", ["GET"]
        elif {"url", "sig"} <= params:
            route, suffix, methods = _signed_stream(fetch), "", ["GET"]
        else:
            continue
        name = module.__name__.rsplit(".", 1)[1]
        app.add_api_route(
            f"/{name}_proxy{suffix}", route, methods=methods, name=f"{name}_proxy", include_in_schema=False,
        )
        prefixes.append(f"/{name}_proxy")
    if prefixes:
        logger.info("registered %d overlay stream relay(s)", len(prefixes))
    return tuple(prefixes)
