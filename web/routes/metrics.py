"""The Prometheus scrape endpoint.

``/metrics`` is not public: per-source success rates, pool saturation and queue
depths tell an observer which sources are dark and how close the database is to
its ceiling. The route enforces its own auth, in order:

1. ``METRICS_TOKEN`` as ``X-Metrics-Token`` or a bearer, which is what a scrape
   config uses.
2. An admin session bearer, so a browser reaches the same data without a second
   secret.

Without ``METRICS_TOKEN`` only an admin session works, so forgetting to configure
it leaves the endpoint more closed, never open.

The path is whitelisted on the login wall purely so case 1 can reach this handler
at all; the wall delegates the decision here rather than skipping it.
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
        # compare_digest, not ==: a shared secret on an endpoint anyone can
        # reach should not leak length or prefix through timing.
        if presented and hmac.compare_digest(presented, token):
            return True
        if bearer and hmac.compare_digest(bearer, token):
            return True

    if bearer:
        # Only reached when the bearer was not the metrics token, so a token
        # scrape never costs a database round-trip.
        user = await run_in_threadpool(account_store.get_user_by_session, bearer)
        if user and user.get("is_admin"):
            return True

    return False


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    """Prometheus text exposition for this replica.

    These counters are per process and reset when a task is rescheduled, so scrape
    the individual Swarm tasks rather than the service VIP. Otherwise consecutive
    scrapes land on different replicas and every counter looks like it is
    sawtoothing."""
    if not observability.PROMETHEUS_AVAILABLE:
        # The dependency is optional by design, so an image built without it says
        # so plainly instead of 404ing as if the feature never existed.
        raise HTTPException(
            status_code=503, detail="prometheus_client is not installed in this build"
        )

    if not await _authorized(request):
        raise HTTPException(status_code=401, detail="Metrics access requires a token or an admin session")

    # render_metrics() runs the state collector, which reads the database, so it
    # goes off the event loop like every other query here.
    payload, content_type = await run_in_threadpool(observability.render_metrics)
    return Response(content=payload, media_type=content_type)
