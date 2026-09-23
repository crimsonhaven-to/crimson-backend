"""The anonymous resolve beacon the client engine sends."""

import asyncio
import logging

from fastapi import APIRouter, Request

from core.rate_limit import limiter

from .db import store

logger = logging.getLogger("crimson.telemetry")

router = APIRouter(tags=["telemetry"])


@router.post("/telemetry/resolve")
@limiter.limit("60/minute")
async def telemetry_resolve(request: Request):
    """Aggregate only: no title, user or IP is stored. Always 200, so the client
    can fire and forget."""
    try:
        events = (await request.json() or {}).get("events") or []
    except Exception:
        events = []
    rows = 0
    if isinstance(events, list) and events:
        try:
            rows = await asyncio.to_thread(store.record_batch, events)
        except Exception as e:
            logger.warning(f"telemetry ingest failed: {e}")
    return {"ok": True, "recorded": rows}
