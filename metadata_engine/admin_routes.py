"""The forced mapping resync and the TMDB catalogue backfill.

The backfill request usually lands on a serving replica, which cannot reach the
portless api-sync container that owns the heavy metadata work, so it is queued in
the database and api-sync drains it. Its status reads back off that row, so it
is correct from any replica.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from account_engine.audit import admin_identity
from account_engine.deps import require_admin
from core.config import get_settings

from . import forced_resync, maintenance

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class BackfillTrigger(BaseModel):
    # TMDB discover stops at page 500, about 20 rows each.
    pages: Optional[int] = Field(None, ge=1, le=500)


@router.get("/resync/status")
async def resync_status():
    return {"success": True, "resync": forced_resync.state}


@router.post("/resync")
async def trigger_resync(user: dict = Depends(require_admin)):
    """The same wholesale rebuild as ``metadata_engine.resync``, in the
    background. Poll /admin/resync/status."""
    if not forced_resync.start(admin_identity(user)):
        return {
            "success": False,
            "message": "A resync is already running",
            "resync": forced_resync.state,
        }
    return {"success": True, "message": "Resync started", "resync": forced_resync.state}


@router.get("/backfill/status")
async def backfill_status():
    row = await asyncio.to_thread(maintenance.latest_backfill_job)
    return {
        "success": True,
        "backfill": maintenance.job_status_payload(row),
        "default_pages": get_settings().metadata_backfill_pages,
    }


@router.post("/backfill")
async def trigger_backfill(
    body: Optional[BackfillTrigger] = None, user: dict = Depends(require_admin)
):
    """Queued in the database and claimed within about a minute by api-sync.
    A no-op while one is already queued or running."""
    pages = (body and body.pages) or get_settings().metadata_backfill_pages
    row, created = await asyncio.to_thread(
        maintenance.request_backfill, pages, admin_identity(user)
    )
    payload = maintenance.job_status_payload(row)
    if not created:
        return {
            "success": False,
            "message": "A backfill is already queued or running",
            "backfill": payload,
        }
    return {
        "success": True,
        "message": "Backfill queued; api-sync will start it shortly",
        "backfill": payload,
    }
