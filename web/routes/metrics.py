"""The Prometheus scrape endpoint.

``/metrics` is **not public**. Per-source success rates, pool saturation and
worker queue depths are operational intelligence: they tell an observer which
sources are currently dark and how close the database is to its ceiling. So the
route enforces its own auth, in this order:

1. ``METRICS_TOKEN`` presented as ``X-Metrics-Token`` or ``Authorization: Bearer``.
   This is the path a Prometheus scrape config uses.
2. An admin session bearer, so the same data is reachable from a browser without
   provisioning a second secret.

With no ``METRICS_TOKEN`` set, only an admin session works. That is the safe
default: forgetting to configure the token makes the endpoint *more* closed, never
open.

The path is whitelisted on the login wall (``_PUBLIC_EXACT`` in ``api.py``) purely
so option 1 can reach this handler at all; the wall delegates the decision here
rather than skipping it.
"""

import hmac
import logging

from fastapi import APIRouter, HTTPException
from fastapi.requests import Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from account_engine import store as account_store
from core import observability

logger = logging.getLogger("crimson.metrics")

router = APIRouter()


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if value[:7].lower() == "bearer ":
        return value.split(" ", 1)[1].strip()
    return ""


async def _authorized(request: Request) -> bool:
    token = observability.metrics_token()
    bearer = _bearer(request)

    if token:
        presented = request.headers.get("x-metrics-token", "").strip()
        # compare_digest, not ==: this is a shared secret checked on an endpoint
        # anyone can reach, so the comparison should not leak length or prefix.
        if presented and hmac.compare_digest(presented, token):
            return True
        if bearer and hmac.compare_digest(bearer, token):
            return True

    if bearer:
        # Falls through to a session lookup only when the bearer was not the
        # metrics token, so a token scrape never costs a database round-trip.
        user = await run_in_threadpool(account_store.get_user_by_session, bearer)
        if user and user.get("is_admin"):
            return True

    return False


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    """Prometheus text exposition for THIS replica.

    Note for the scrape config: these counters are per replica and per process,
    and they reset when a task is rescheduled. Scrape the individual Swarm tasks
    (``tasks.<service>``) rather than the service VIP, otherwise consecutive
    scrapes land on different replicas and every counter looks like it is
    sawtoothing."""
    if not observability.PROMETHEUS_AVAILABLE:
        # The dependency is optional by design (see core/observability.py), so an
        # image built without it says so plainly instead of 404ing as if the
        # feature did not exist.
        raise HTTPException(
            status_code=503, detail="prometheus_client is not installed in this build"
        )

    if not await _authorized(request):
        raise HTTPException(status_code=401, detail="Metrics access requires a token or an admin session")

    # render_metrics() runs the state collector, which reads the database. Same
    # rule as every other query in this codebase: keep it off the event loop.
    payload, content_type = await run_in_threadpool(observability.render_metrics)
    return Response(content=payload, media_type=content_type)
