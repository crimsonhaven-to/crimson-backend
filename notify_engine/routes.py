"""
The airing calendar and per-title subscriptions.

  * ``GET    /calendar``               what airs in a window, the caller's follows flagged
  * ``GET    /account/subscriptions``  what the caller follows, with each next episode
  * ``POST   /account/subscriptions``  follow a title
  * ``DELETE /account/subscriptions/{anilist_id}``

All four sit behind the site-wide login wall and resolve the caller with
``require_user``, the same dependency the rest of the account surface uses.

Subscribing works for every account, including a mnemonic one with no email
address. The calendar is worth having on its own, and refusing the follow would
be a strange way to say "we cannot mail you". The response reports
``email_notifications`` so the client can say so plainly instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from account_engine.deps import require_user

from .db import store

logger = logging.getLogger("crimson.airing")

router = APIRouter(tags=["airing"])

# A calendar is read a week at a time; a month is the most that is meaningful and
# bounds the response without needing pagination.
_MAX_DAYS = 31


class SubscriptionIn(BaseModel):
    anilist_id: int = Field(..., ge=1)
    # Snapshotted on the subscription: see migrations/004_airing.sql.
    title: Optional[str] = Field(None, max_length=500)
    poster: Optional[str] = Field(None, max_length=1000)
    notify_email: bool = True


def _can_be_emailed(user: dict) -> bool:
    return bool(user.get("email")) and bool(user.get("email_verified"))


@router.get("/calendar")
async def get_calendar(
    days: int = Query(7, ge=1, le=_MAX_DAYS, description="Days forward from the start of today"),
    back: int = Query(1, ge=0, le=_MAX_DAYS, description="Days of already-aired episodes to include"),
    user: dict = Depends(require_user),
):
    """What airs in the window, with the caller's own follows flagged.

    Both halves in one response: the client draws the whole schedule and
    highlights what the caller follows, so splitting it would mean two requests
    to render one view.
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    items = await run_in_threadpool(store.calendar, start, end, user["user_id"])
    return {
        "success": True,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(items),
        "items": items,
    }


@router.get("/account/subscriptions")
async def list_subscriptions(user: dict = Depends(require_user)):
    items = await run_in_threadpool(store.list_subscriptions, user["user_id"])
    return {
        "success": True,
        "count": len(items),
        # So the client can explain why a follow will not mail, rather than
        # leaving the user to wonder why nothing ever arrives.
        "email_notifications": _can_be_emailed(user),
        "subscriptions": items,
    }


@router.post("/account/subscriptions")
async def add_subscription(body: SubscriptionIn, user: dict = Depends(require_user)):
    await run_in_threadpool(
        store.subscribe, user["user_id"], body.anilist_id,
        body.title, body.poster, body.notify_email,
    )
    return {
        "success": True,
        "anilist_id": body.anilist_id,
        "notify_email": body.notify_email,
        "email_notifications": _can_be_emailed(user),
    }


@router.delete("/account/subscriptions/{anilist_id}")
async def remove_subscription(anilist_id: int, user: dict = Depends(require_user)):
    removed = await run_in_threadpool(store.unsubscribe, user["user_id"], anilist_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Not subscribed to that title")
    return {"success": True, "anilist_id": anilist_id}
