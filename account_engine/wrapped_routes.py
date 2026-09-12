"""
Crimson Wrapped: the year-in-review endpoint.

    GET /account/wrapped?year=2026&offset_minutes=-480

``offset_minutes`` is the viewer's own UTC offset, because "busiest day" and
"longest streak" are the two stats that change meaning with where you are. The
client sends what its browser reports; omitting it answers in UTC.

The aggregation and every counting rule live in account_engine.wrapped.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from . import wrapped
from .routes import require_user

router = APIRouter(tags=["account-wrapped"])

# watch_events is pruned at three years, so anything older has no exact source
# left and only the approximate one, which for a year that old is not worth
# presenting.
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
    """This account's year of watching.

    Check ``approximate`` before presenting any of it as exact: it is true when
    part of the year predates the event table, and ``events_since`` says where
    the reliable part starts."""
    if year is None:
        year = datetime.now(timezone.utc).year
    year = max(_EARLIEST_YEAR, min(datetime.now(timezone.utc).year, year))
    stats = await run_in_threadpool(wrapped.build, user["user_id"], year, offset_minutes)
    return {"success": True, **stats}
