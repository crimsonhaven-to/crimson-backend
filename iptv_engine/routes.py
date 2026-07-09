"""
IPTV API — the Live TV surface over the iptv-org public index.

Browse/search/detail are behind the login wall like every other content route;
``/iptv_proxy`` is public + HMAC-signed (hls.js loads it cross-origin and can't
carry the bearer — same reasoning as /subtitles_proxy and /local_art).

All routes answer 503 when the surface is disabled (``IPTV_ENABLED=false``).
While the catalogue is still warming (first boot), the read routes answer
``ready: false`` instead of blocking on a ~25 MB fetch — the frontend shows its
tuning state and asks again.
"""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from core import lumi
from web.routes.proxies import _proxy_response

from .service import IptvService, enabled, proxy_fetch, verify_stream_sig

logger = logging.getLogger("crimson.iptv")

router = APIRouter(tags=["iptv"])
service = IptvService()


def _require_enabled() -> None:
    if not enabled():
        raise HTTPException(status_code=503, detail="Live TV is not enabled on this haven")


def _warming_payload() -> dict:
    """Shared not-ready shape: the catalogue is being summoned, ask again."""
    st = service.status()
    return {
        "success": True,
        "ready": False,
        "total": 0,
        "error": st["error"],
        "message": "Lumi is tuning the crimson airwaves — ask again in a moment, darling.",
    }


@router.get("/iptv/browse")
async def iptv_browse():
    """The browse facets: categories + countries (with channel counts) and the
    catalogue total. Drives the Live TV hub's filter chips."""
    _require_enabled()
    service.ensure_refresh_started()
    if not service.ready:
        return {**_warming_payload(), "categories": [], "countries": []}
    facets = await run_in_threadpool(service.browse_facets)
    st = service.status()
    return {
        "success": True,
        "ready": True,
        "refreshed_at": st["refreshed_at"],
        **facets,
    }


@router.get("/iptv/channels")
async def iptv_channels(
    category: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=200),
):
    """Paged channel cards, filtered by category/country and/or a search term."""
    _require_enabled()
    service.ensure_refresh_started()
    if not service.ready:
        return {**_warming_payload(), "channels": [], "page": page, "page_size": page_size}
    result = await run_in_threadpool(
        service.list_channels, category, country, q, page, page_size
    )
    return {"success": True, "ready": True, **result}


@router.get("/iptv/channel/{channel_id}")
async def iptv_channel(channel_id: str):
    """Full channel detail for the watch page — every known stream, best
    quality first, each with its signed same-origin proxy path."""
    _require_enabled()
    service.ensure_refresh_started()
    if not service.ready:
        return {**_warming_payload(), "channel": None}
    channel = await run_in_threadpool(service.get_channel, channel_id)
    if not channel:
        raise HTTPException(
            status_code=404,
            detail="No such channel graces the crimson airwaves",
        )
    return {"success": True, "ready": True, "channel": channel}


@router.get("/iptv_proxy")
async def iptv_proxy(
    request: Request,
    u: str = Query(..., description="Upstream URL (signed)"),
    s: str = Query(..., description="HMAC signature"),
    r: str = Query("", description="Upstream Referer (covered by the signature)"),
    a: str = Query("", description="Upstream User-Agent (covered by the signature)"),
):
    """Signed same-origin relay for IPTV playlists + segments.

    Public (hls.js can't attach the login-wall bearer cross-origin) but never an
    open relay: the URL *and* the header overrides are HMAC-signed, and the
    fetch runs through the SSRF-guarded client (untrusted hosts + redirects).
    Playlists come back rewritten so every sub-resource flows through here too.
    """
    _require_enabled()
    if not verify_stream_sig(u, s, r, a):
        raise HTTPException(status_code=403, detail=lumi.voiced_error(403))
    try:
        result = await proxy_fetch(u, r, a, range_header=request.headers.get("range"))
    except ValueError as e:  # SSRFError included
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.warning(f"IPTV upstream fetch failed for {u}: {e}")
        raise HTTPException(status_code=502, detail="Upstream broadcast unreachable")
    return _proxy_response(*result)
