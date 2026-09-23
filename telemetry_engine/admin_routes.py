import asyncio

from fastapi import APIRouter, Depends, Query

from account_engine.deps import require_admin
from core.clock import utc_now_iso

from .db import store

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/source-stats")
async def admin_source_stats(days: int = Query(14, ge=1, le=365, description="Window to aggregate over")):
    """What actually resolved for viewers, from their anonymous beacons, where
    /admin/source-health probes from the backend."""
    rows = await asyncio.to_thread(store.top_stats, days)
    return {"success": True, "generated_at": utc_now_iso(), "days": days, "sources": rows}
