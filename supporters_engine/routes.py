"""Ko-fi webhook ingest and the public "Lumi's Loved Mortals" list.

``POST /kofi/webhook`` is called by Ko-fi only. ``GET /supporters`` and
``GET /supporters/stats`` are public and unauthenticated; the frontend renders
the supporters page from them.
"""

import json
import secrets
from typing import Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from core.config import get_settings

from . import service

router = APIRouter(tags=["supporters"])


@router.post("/kofi/webhook")
async def kofi_webhook(request: Request):
    """A duplicate still answers 200 so Ko-fi stops retrying."""
    expected = get_settings().kofi_verification_token
    if not expected:
        raise HTTPException(status_code=503, detail="Ko-fi webhook not configured")

    # Parsed by hand rather than request.form(), which would pull in
    # python-multipart just for one urlencoded field.
    body = (await request.body()).decode("utf-8", "replace")
    values = parse_qs(body).get("data")
    raw = values[0] if values else None
    if not raw:
        raise HTTPException(status_code=400, detail="Missing 'data' field")

    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed 'data' JSON")
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Malformed 'data' JSON")

    token = event.get("verification_token") or ""
    if not secrets.compare_digest(str(token), expected):
        raise HTTPException(status_code=401, detail="Invalid verification token")

    inserted = await run_in_threadpool(service.record_payment, event)
    return {"success": True, "recorded": inserted}


@router.get("/supporters")
async def list_supporters(
    include_lapsed: bool = Query(
        False, description="Include subscribers whose membership has lapsed."),
    limit: Optional[int] = Query(
        None, ge=1, le=1000, description="Cap the number of supporters returned."),
):
    """Most recent payment first."""
    supporters = await run_in_threadpool(service.list_public, include_lapsed, limit)
    return {"success": True, "count": len(supporters), "supporters": supporters}


@router.get("/supporters/stats")
async def supporters_stats():
    return {"success": True, **await run_in_threadpool(service.stats)}
