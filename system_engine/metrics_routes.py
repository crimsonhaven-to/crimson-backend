"""The Prometheus scrape.

Not public: per-source success rates and pool saturation tell an observer which
sources are dark and how close the database is to its ceiling. A scrape presents
``METRICS_TOKEN`` (header or bearer); a browser uses an admin session. Without a
token configured only admins get in, so forgetting it fails closed. The path is
exempt from the login wall only so a token scrape can reach this check.
"""

import asyncio
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from account_engine.db import store as account_store
from account_engine.deps import bearer_token
from core import metrics
from core.config import Settings, get_settings

router = APIRouter(tags=["system"])


async def _authorized(request: Request, bearer: str | None, token: str) -> bool:
    if token:
        presented = request.headers.get("x-metrics-token", "").strip()
        # compare_digest, so a public endpoint leaks no length or prefix by timing.
        if presented and hmac.compare_digest(presented, token):
            return True
        if bearer and hmac.compare_digest(bearer, token):
            return True
    if bearer:
        # Reached only when the bearer was not the token, so a scrape costs no query.
        user = await asyncio.to_thread(account_store.get_user_by_session, bearer)
        return bool(user and user.get("is_admin"))
    return False


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    request: Request,
    bearer: str | None = Depends(bearer_token),
    settings: Settings = Depends(get_settings),
):
    """Per replica, and counters reset on reschedule: scrape the Swarm tasks, not
    the service VIP, or consecutive scrapes hit different replicas and every
    counter sawtooths."""
    if not await _authorized(request, bearer, settings.metrics_token):
        raise HTTPException(
            status_code=401, detail="Metrics access requires a token or an admin session"
        )
    payload, content_type = await asyncio.to_thread(metrics.render)
    return Response(content=payload, media_type=content_type)
