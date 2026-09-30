"""What a member listened to: the player's reports in, the year back out for
Crimson Wrapped. The counting rules live in listens.py and wrapped.py."""

import asyncio

from fastapi import APIRouter, Depends, Query, Request

from account_engine.wrapped import MAX_OFFSET_MINUTES, MIN_OFFSET_MINUTES
from core.clock import utc_now
from core.public_url import public_base_url
from core.rate_limit import limiter

from . import listens, wrapped
from .access import require_music_user
from .payloads import track_payload
from .schemas import ListenBatch

router = APIRouter(prefix="/music", tags=["music-listening"])


@router.post("/listens")
@limiter.limit("30/minute")
async def report_listens(
    request: Request, body: ListenBatch, user: dict = Depends(require_music_user)
):
    reported = [(item.track_id, item.listened_at, item.seconds) for item in body.listens]
    recorded = await asyncio.to_thread(listens.record, user["user_id"], reported)
    return {"success": True, "recorded": recorded}


@router.get("/wrapped")
async def music_wrapped(
    request: Request,
    user: dict = Depends(require_music_user),
    year: int = Query(None, ge=2000, le=2100, description="Calendar year; defaults to the current one"),
    offset_minutes: int = Query(
        0,
        ge=MIN_OFFSET_MINUTES,
        le=MAX_OFFSET_MINUTES,
        description="The viewer's UTC offset in minutes, as JS getTimezoneOffset() negated",
    ),
):
    """This account's year of listening, counted in the viewer's timezone."""
    year = year if year is not None else utc_now().year
    stats = await asyncio.to_thread(wrapped.build, user["user_id"], year, offset_minutes)
    base = public_base_url(request).rstrip("/")
    return {
        "success": True,
        "year": year,
        **stats,
        "top_tracks": [
            {"track": track_payload(entry["row"], base), "plays": entry["plays"],
             "minutes": entry["minutes"]}
            for entry in stats["top_tracks"]
        ],
    }
