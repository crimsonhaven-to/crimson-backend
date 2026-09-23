"""Prometheus metrics recorded from the hot paths.

Two rules hold throughout. Recording never raises into a caller, because these
sit inside the /watch fan-out and the resolver loop. And every label value comes
from a fixed vocabulary or is capped, because an unbounded label is how a
metrics endpoint becomes an outage.

A private registry keeps the export self-contained, and lets a test re-import
the module without a duplicate-timeseries error.
"""

import logging
from typing import Optional, Tuple

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    GCCollector,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

logger = logging.getLogger("crimson.metrics")

REGISTRY = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)

_KNOWN_METHODS = frozenset(("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"))
# A request that matched no route; otherwise one scanner mints a series per URL.
UNMATCHED_ROUTE = "__unmatched__"

HTTP_REQUESTS = Counter(
    "crimson_http_requests_total",
    "HTTP requests completed, by route template, method and status class.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "crimson_http_request_duration_seconds",
    # To the headers, not the last byte: /watch streams for as long as the
    # slowest scraper, which would make every percentile meaningless.
    "Seconds from request start to response headers (NOT to last byte).",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
HTTP_IN_PROGRESS = Gauge(
    "crimson_http_requests_in_progress",
    "Requests currently being handled by this replica.",
    ("method",),
    registry=REGISTRY,
)
WATCH_REQUESTS = Counter(
    "crimson_watch_requests_total",
    "Completed /watch fan-outs by outcome (streams / empty / unaired / error).",
    ("media_type", "outcome"),
    registry=REGISTRY,
)
WATCH_FIRST_STREAM = Histogram(
    "crimson_watch_first_stream_seconds",
    "Seconds from /watch fan-out start until the FIRST playable stream is emitted.",
    ("media_type",),
    buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60),
    registry=REGISTRY,
)
WATCH_DURATION = Histogram(
    "crimson_watch_duration_seconds",
    "Seconds from /watch fan-out start until every scraper has finished.",
    ("media_type",),
    buckets=(1, 2, 3, 5, 8, 12, 20, 30, 60, 120),
    registry=REGISTRY,
)
WATCH_STREAMS = Counter(
    "crimson_watch_streams_total",
    "Individual playable streams emitted across all /watch fan-outs.",
    ("media_type",),
    registry=REGISTRY,
)
SCRAPER_RUNS = Counter(
    "crimson_scraper_runs_total",
    "Scraper discovery runs by outcome (embeds / empty / error).",
    ("scraper", "outcome"),
    registry=REGISTRY,
)
SCRAPER_DURATION = Histogram(
    "crimson_scraper_duration_seconds",
    "Seconds spent in one scraper's search + embed discovery.",
    ("scraper",),
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30),
    registry=REGISTRY,
)
RESOLVE_RUNS = Counter(
    "crimson_resolve_total",
    "Embed resolve attempts by source and outcome (ok / empty / error).",
    ("source", "outcome"),
    registry=REGISTRY,
)
RESOLVE_DURATION = Histogram(
    "crimson_resolve_duration_seconds",
    "Seconds spent resolving one embed to a direct stream.",
    ("source",),
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30),
    registry=REGISTRY,
)
RESPONSE_CACHE = Counter(
    "crimson_response_cache_total",
    "Response-cache lookups by tier (l1 in-process / l2 postgres) and result.",
    ("tier", "result"),
    registry=REGISTRY,
)


def method_label(method: Optional[str]) -> str:
    m = (method or "").upper()
    return m if m in _KNOWN_METHODS else "OTHER"


def route_label(scope: dict) -> str:
    """The route template, never the raw path, which would mint a series per
    episode of every show. Starlette sets ``scope["route"]`` while routing, so
    this is only meaningful once the response has started."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template[:200] if isinstance(template, str) and template else UNMATCHED_ROUTE


def _safely(record, *args) -> None:
    try:
        record(*args)
    except Exception as e:
        logger.debug(f"metric not recorded: {e}")


def _http(method, route, status, duration):
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(duration)


def record_http_request(method: str, route: str, status: int, duration: float) -> None:
    _safely(_http, method, route, status, duration)


def track_in_progress(method: str, delta: int) -> None:
    _safely(lambda: HTTP_IN_PROGRESS.labels(method).inc(delta))


def _scraper(scraper, outcome, duration):
    SCRAPER_RUNS.labels(scraper, outcome).inc()
    SCRAPER_DURATION.labels(scraper).observe(duration)


def record_scraper_run(scraper: str, outcome: str, duration: float) -> None:
    _safely(_scraper, scraper, outcome, duration)


def _resolve(source, outcome, duration):
    RESOLVE_RUNS.labels(source, outcome).inc()
    RESOLVE_DURATION.labels(source).observe(duration)


def record_resolve(source: str, outcome: str, duration: float) -> None:
    _safely(_resolve, source, outcome, duration)


def _watch(media_type, outcome, stream_count, duration, first_stream):
    WATCH_REQUESTS.labels(media_type, outcome).inc()
    WATCH_DURATION.labels(media_type).observe(duration)
    if stream_count:
        WATCH_STREAMS.labels(media_type).inc(stream_count)
    if first_stream is not None:
        WATCH_FIRST_STREAM.labels(media_type).observe(first_stream)


def record_watch(
    media_type: str,
    outcome: str,
    stream_count: int,
    duration: float,
    first_stream: Optional[float] = None,
) -> None:
    """One /watch fan-out. ``first_stream`` is None when nothing resolved."""
    _safely(_watch, media_type, outcome, stream_count, duration, first_stream)


def record_cache_lookup(tier: str, hit: bool) -> None:
    _safely(lambda: RESPONSE_CACHE.labels(tier, "hit" if hit else "miss").inc())


def render() -> Tuple[bytes, str]:
    """Blocking: the state collector reads the database, so run it off the loop."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
