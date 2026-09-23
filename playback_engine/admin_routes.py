from fastapi import APIRouter, Depends, Query

from account_engine.deps import require_admin

from . import health
from .health import datetime, timezone

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/source-health")
async def admin_source_health(
    force: bool = Query(False, description="Bypass the short result cache and re-probe now"),
):
    return {
        "success": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **await health.source_health(force),
    }
