"""Playback of cached episodes, and the beacon that decides what gets cached."""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.rate_limit import limiter

from .downloader import manager
from .fs import media_type_for, safe_resolve

router = APIRouter(tags=["cache"])


@router.get("/cache_proxy/{token}")
async def cache_proxy(token: str):
    """A token maps to a file only while it sits in an enabled cache target."""
    real_path = await asyncio.to_thread(safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real_path, media_type=media_type_for(real_path))


@router.post("/cache/confirm")
@limiter.limit("120/minute")
async def confirm_cache(request: Request):
    """Redeem a ``cacheTicket`` after about ten seconds of real playback, so the
    cached source is the one the viewer chose rather than whichever resolved
    first. The ticket is signed by /watch, so no arbitrary URL reaches the
    downloader. Always 200, so it never reveals whether caching is on."""
    try:
        ticket = (await request.json() or {}).get("ticket") or ""
    except Exception:
        ticket = ""
    return {"ok": bool(await manager.confirm_ticket(ticket)) if ticket else False}
