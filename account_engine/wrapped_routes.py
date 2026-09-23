"""Crimson Wrapped, the year-in-review endpoint. The counting rules live in wrapped.py."""

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from core.clock import utc_now

from . import wrapped
from .deps import require_user

router = APIRouter(tags=["account-wrapped"])

# watch_events is pruned at three years, and an older year would come only from
# the approximate source, which is not worth presenting.
_EARLIEST_YEAR = 2023


@router.get("/account/wrapped")
async def get_wrapped(
    user: dict = Depends(require_user),
    year: int = Query(None, description="Calendar year; defaults to the current one"),
    offset_minutes: int = Query(
        0,
        ge=wrapped.MIN_OFFSET_MINUTES,
        le=wrapped.MAX_OFFSET_MINUTES,
        description="The viewer's UTC offset in minutes, as JS getTimezoneOffset() negated",
    ),
):
    """This account's year of watching, with "busiest day" and "longest streak"
    in the viewer's timezone. Check ``approximate`` before presenting any of it
    as exact; ``events_since`` says where the reliable part starts."""
    current = utc_now().year
    year = max(_EARLIEST_YEAR, min(current, year if year is not None else current))
    stats = await run_in_threadpool(wrapped.build, user["user_id"], year, offset_minutes)
    return {"success": True, **stats}
