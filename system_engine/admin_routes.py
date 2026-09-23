"""The admin System tab and the Metrics tab's history.

/metrics is one replica's live snapshot; the history reads a Prometheus that
scrapes them all. Panels and their PromQL are fixed in core/prom_query.py: the
browser sends a panel id, a dictionary key, never a query.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from account_engine.deps import require_admin
from core import prom_query
from core.clock import utc_now_iso
from core.rate_limit import limiter

from . import info

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _require_prometheus() -> None:
    if not prom_query.available():
        raise HTTPException(
            status_code=503, detail="No Prometheus is configured (set PROMETHEUS_URL)"
        )


@router.get("/system")
async def admin_system():
    return {"success": True, "generated_at": utc_now_iso(), "system": await info.snapshot()}


@router.get("/metrics/panels")
async def metrics_panels():
    """``available: false`` is the normal answer without Prometheus; the tab then
    shows only the live snapshot."""
    if not prom_query.available():
        return {
            "success": True,
            "available": False,
            "reason": "PROMETHEUS_URL is not set, so no history is being collected",
            "panels": [],
            "ranges": [],
        }
    return {
        "success": True,
        "available": True,
        "job": prom_query.JOB,
        "retention": await prom_query.retention_hint(),
        "panels": prom_query.panel_catalogue(),
        "ranges": prom_query.range_catalogue(),
        "default_range": prom_query.DEFAULT_RANGE,
    }


@router.get("/metrics/series", dependencies=[Depends(_require_prometheus)])
# Generous: opening the tab fires one call per panel and changing the range
# refires them all. The limit only stops a stuck client looping on Prometheus.
@limiter.limit("240/minute")
async def metrics_series(
    request: Request,
    panel: str = Query(..., description="Panel id from /admin/metrics/panels"),
    # Aliased so the parameter does not shadow the builtin.
    range_id: str = Query(
        prom_query.DEFAULT_RANGE, alias="range", description="Range id from /admin/metrics/panels"
    ),
):
    if panel not in prom_query.PANELS:
        raise HTTPException(status_code=404, detail=f"Unknown panel '{panel[:40]}'")
    if range_id not in prom_query.RANGES:
        raise HTTPException(status_code=404, detail=f"Unknown range '{range_id[:40]}'")
    return await prom_query.query_panel(panel, range_id)


@router.get("/metrics/targets", dependencies=[Depends(_require_prometheus)])
async def metrics_targets():
    """The replicas Prometheus scrapes, so an empty chart can be told apart from
    a scraper that lost the fleet."""
    return await prom_query.scrape_targets()
