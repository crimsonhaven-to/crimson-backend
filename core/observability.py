"""Operational metrics and the per-request correlation id.

Two things live here, because they are the same concern seen from two angles:

* **Metrics** (Prometheus): counters/histograms recorded from the hot paths, plus a
  scrape-time collector that reads the live gauges (DB pool, worker queues, source
  success rates) straight from the modules that already own them.
* **The request id**: a short token minted per request, carried in a ``ContextVar``
  so every log line emitted while handling that request can be correlated, and
  echoed back as ``X-Request-ID`` so a user report ("playback broke at 20:15")
  maps to an exact set of log lines.

Three properties this module is built around, all of them load-bearing:

1. **``prometheus_client`` is an optional import.** If it is missing, every
   recording helper degrades to a no-op and ``/metrics`` answers 503. A metrics
   dependency must never be able to stop the backend from booting or serving.
2. **Nothing here may raise into a caller.** These helpers sit inside the /watch
   fan-out and the resolver loop. A broken counter must never break playback, so
   every public entry point swallows its own exceptions.
3. **Label cardinality is bounded on purpose.** Every label value is either from a
   fixed vocabulary or explicitly capped. See ``route_label`` (which uses the route
   *template*, never the raw path) and ``_TELEMETRY_TOP_N``. An unbounded label is
   how a metrics endpoint turns into an outage.

The metrics live in a private ``CollectorRegistry`` rather than the process-global
default one. That keeps the export self-contained, and means re-importing this
module (as a test may) builds a fresh registry instead of raising a duplicate
timeseries error.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from typing import Optional, Tuple

logger = logging.getLogger("crimson.observability")

# --- optional dependency ----------------------------------------------------
try:  # pragma: no cover - trivially environment-dependent
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily
    from prometheus_client import PlatformCollector, ProcessCollector, GCCollector

    PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only on a stripped install
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


# --- request id -------------------------------------------------------------
# Set by RequestContextMiddleware (api.py) for the duration of one request. A
# ContextVar (not a global) so concurrent requests can't see each other's id, and
# so it survives the two ways work leaves the handler: run_in_threadpool copies
# the context into the worker thread, and asyncio.create_task copies it into the
# background task (which is what carries the id into the warmup + cache jobs).
_request_id: ContextVar[str] = ContextVar("crimson_request_id", default="")

# Request ids may arrive from a reverse proxy, so they are untrusted input that
# ends up in log lines and a response header. Restrict hard to an opaque token
# rather than trying to sanitize something structured.
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_ID_MAX_LEN = 64


def new_request_id() -> str:
    """A fresh short correlation id (16 hex chars: greppable, still collision-safe
    at any request rate this backend will ever see)."""
    return uuid.uuid4().hex[:16]


def clean_request_id(raw: Optional[str]) -> str:
    """Normalize an inbound ``X-Request-ID`` into a safe opaque token.

    Returns "" when there is nothing usable, so the caller mints its own. The
    header is attacker-controllable on any deployment where the reverse proxy
    passes it through, hence the strict character class and the length cap: this
    value is written into logs and reflected in a response header."""
    if not raw:
        return ""
    return _ID_SAFE.sub("", raw.strip())[:_ID_MAX_LEN]


def set_request_id(value: str):
    """Bind ``value`` for the current context. Returns the reset token."""
    return _request_id.set(value)


def reset_request_id(token) -> None:
    try:
        _request_id.reset(token)
    except Exception:
        pass


def current_request_id() -> str:
    """The id of the request being handled, or "" outside a request (startup,
    scheduler jobs). Read by the log formatters in ``core.logging_setup``."""
    return _request_id.get()


# --- label helpers ----------------------------------------------------------
_KNOWN_METHODS = frozenset(
    ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
)
# The label used when a request matched no route (404s, and every path a scanner
# invents). Without this collapse, one scanner would mint a new timeseries per
# probed URL and the export would grow without bound.
UNMATCHED_ROUTE = "__unmatched__"


def method_label(method: Optional[str]) -> str:
    m = (method or "").upper()
    return m if m in _KNOWN_METHODS else "OTHER"


def route_label(scope: dict) -> str:
    """The route *template* for this request ("/watch/{tmdb_id}/{season_number}/…"),
    never the raw path.

    This is the single most important cardinality guard in the module: labelling by
    ``scope["path"]`` would mint a timeseries per episode of per show, which is
    unbounded in exactly the dimension this backend is largest in.

    Starlette sets ``scope["route"]`` while routing, so this is only meaningful
    once the response has started (which is where the middleware reads it)."""
    route = scope.get("route")
    path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(path_format, str) and path_format:
        return path_format[:200]
    return UNMATCHED_ROUTE


# --- metric definitions -----------------------------------------------------
# Everything below is guarded by PROMETHEUS_AVAILABLE. When the dependency is
# absent the names simply don't exist and the record_* helpers return early.

if PROMETHEUS_AVAILABLE:
    REGISTRY = CollectorRegistry(auto_describe=True)

    # Process/platform/GC stats. ProcessCollector reads /proc, so it exports
    # nothing on a non-Linux dev box and everything in the container; both are fine.
    for _default in (ProcessCollector, PlatformCollector, GCCollector):
        try:
            _default(registry=REGISTRY)
        except Exception:  # pragma: no cover
            pass

    HTTP_REQUESTS = Counter(
        "crimson_http_requests_total",
        "HTTP requests completed, by route template, method and status class.",
        ("method", "route", "status"),
        registry=REGISTRY,
    )
    HTTP_DURATION = Histogram(
        "crimson_http_request_duration_seconds",
        # Deliberately time-to-headers, not time-to-last-byte: /watch is a
        # progressive NDJSON stream that stays open for as long as the slowest
        # scraper runs, and mixing that into the general latency histogram would
        # make every percentile meaningless. The streaming side is measured
        # separately by the crimson_watch_* metrics below.
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


# --- recording helpers ------------------------------------------------------
# Every one of these is called from a hot path and must be inert on failure. The
# broad excepts are intentional: a metrics bug is not allowed to break playback.


def record_http_request(method: str, route: str, status: int, duration: float) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        HTTP_REQUESTS.labels(method, route, str(status)).inc()
        HTTP_DURATION.labels(method, route).observe(duration)
    except Exception:
        pass


def track_in_progress(method: str, delta: int) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        HTTP_IN_PROGRESS.labels(method).inc(delta)
    except Exception:
        pass


def record_scraper_run(scraper: str, outcome: str, duration: float) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        SCRAPER_RUNS.labels(scraper, outcome).inc()
        SCRAPER_DURATION.labels(scraper).observe(duration)
    except Exception:
        pass


def record_resolve(source: str, outcome: str, duration: float) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        RESOLVE_RUNS.labels(source, outcome).inc()
        RESOLVE_DURATION.labels(source).observe(duration)
    except Exception:
        pass


def record_watch(
    media_type: str,
    outcome: str,
    stream_count: int,
    duration: float,
    first_stream: Optional[float] = None,
) -> None:
    """One completed /watch fan-out. ``first_stream`` is None when nothing resolved."""
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        WATCH_REQUESTS.labels(media_type, outcome).inc()
        WATCH_DURATION.labels(media_type).observe(duration)
        if stream_count:
            WATCH_STREAMS.labels(media_type).inc(stream_count)
        if first_stream is not None:
            WATCH_FIRST_STREAM.labels(media_type).observe(first_stream)
    except Exception:
        pass


def record_cache_lookup(tier: str, hit: bool) -> None:
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        RESPONSE_CACHE.labels(tier, "hit" if hit else "miss").inc()
    except Exception:
        pass


class Timer:
    """Monotonic stopwatch. ``with Timer() as t: ...`` then read ``t.elapsed``.

    Used instead of bare ``time.monotonic()`` pairs so an early ``return`` or a
    raised exception inside an instrumented block still yields a duration."""

    __slots__ = ("_start", "elapsed")

    def __init__(self) -> None:
        self._start = time.monotonic()
        self.elapsed = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc) -> bool:
        self.elapsed = time.monotonic() - self._start
        return False

    def lap(self) -> float:
        return time.monotonic() - self._start


# --- scrape-time collector --------------------------------------------------
# Live values that are already owned by another module (pool counters, queue
# depths, the telemetry table). Reading them at scrape time rather than polling
# them on a timer means there is no background job to keep alive, and the numbers
# are exactly as fresh as the scrape.

# Cap on how many distinct sources the telemetry gauge exports. The
# resolve_telemetry table is fed by a CLIENT beacon, and a source name there is
# client-supplied text (capped at 80 chars, but not to a fixed vocabulary). A
# hostile client could therefore invent unlimited source names; without this cap
# each one would become a permanent timeseries. top_stats() already orders by
# volume, so the busiest real sources always survive the slice.
_TELEMETRY_TOP_N = 25

# The telemetry gauge is the only part of the collector that hits the database.
# Prometheus scrapes far more often than these daily aggregates change, so the
# result is memoized for a scrape interval or two.
_TELEMETRY_TTL = 60.0
_telemetry_cache: Tuple[float, list] = (0.0, [])


def _telemetry_rows() -> list:
    global _telemetry_cache
    cached_at, rows = _telemetry_cache
    now = time.monotonic()
    if rows and now - cached_at < _TELEMETRY_TTL:
        return rows
    from web.context import telemetry_store

    rows = telemetry_store.top_stats(days=14)[:_TELEMETRY_TOP_N]
    _telemetry_cache = (now, rows)
    return rows


class CrimsonStateCollector:
    """Exports live subsystem state at scrape time.

    Each section is independently try/excepted: a database outage must degrade the
    export to "the DB gauges are missing", not to a 500 on /metrics that blinds
    the operator at exactly the moment they need it most."""

    def collect(self):  # noqa: C901 - a flat list of independent sections
        if not PROMETHEUS_AVAILABLE:
            return

        # --- build + schema ------------------------------------------------
        try:
            from core.version import VERSION
            from core import migrations

            info = GaugeMetricFamily(
                "crimson_build_info",
                "Always 1; the labels carry the running version.",
                labels=["version"],
            )
            info.add_metric([str(VERSION)], 1)
            yield info

            schema = migrations.cached_status() or {}
            version = schema.get("version")
            if version is not None:
                g = GaugeMetricFamily(
                    "crimson_schema_version",
                    "Schema migration version this replica booted at. A rolling "
                    "deploy that leaves replicas disagreeing shows up here.",
                )
                g.add_metric([], float(version))
                yield g
            drift = GaugeMetricFamily(
                "crimson_schema_drift",
                "Migration files whose checksum no longer matches what was applied.",
            )
            drift.add_metric([], float(len(schema.get("drift") or [])))
            yield drift
        except Exception:
            pass

        # --- database pool --------------------------------------------------
        try:
            from core.db_pool import pool_stats

            stats = pool_stats()
            if stats.get("available"):
                for key, doc in (
                    ("size", "Connections currently held by the pool."),
                    ("idle", "Pooled connections available right now."),
                    ("in_use", "Pooled connections currently checked out."),
                    ("waiting", "Requests blocked waiting for a connection."),
                    ("max_size", "Configured pool ceiling."),
                    ("min_size", "Configured pool floor."),
                ):
                    value = stats.get(key)
                    if value is None:
                        continue
                    g = GaugeMetricFamily(f"crimson_db_pool_{key}", doc)
                    g.add_metric([], float(value))
                    yield g
                for key, name, doc in (
                    ("requests_total", "crimson_db_pool_requests_total",
                     "Connection requests served by the pool."),
                    ("requests_errors", "crimson_db_pool_request_errors_total",
                     "Connection requests that failed."),
                    ("connections_total", "crimson_db_pool_connections_total",
                     "Connections opened by the pool."),
                ):
                    value = stats.get(key)
                    if value is None:
                        continue
                    c = CounterMetricFamily(name, doc)
                    c.add_metric([], float(value))
                    yield c
        except Exception:
            pass

        # --- background workers ---------------------------------------------
        # Only the dedicated worker replicas actually run these; on an api replica
        # they report 0, which is correct rather than missing.
        try:
            from cache_engine.downloader import manager as cache_manager

            stats = cache_manager.worker_stats()
            depth = GaugeMetricFamily(
                "crimson_cache_worker_queue_depth",
                "Remux jobs queued in this replica's cache worker.",
            )
            depth.add_metric([], float(stats.get("queued", 0)))
            yield depth
            inflight = GaugeMetricFamily(
                "crimson_cache_worker_inflight",
                "Remux jobs currently running in this replica's cache worker.",
            )
            inflight.add_metric([], float(stats.get("inflight", 0)))
            yield inflight
        except Exception:
            pass

        try:
            from download_engine.manager import manager as download_manager

            stats = download_manager.worker_stats()
            g = GaugeMetricFamily(
                "crimson_download_jobs",
                "Admin download jobs by status. CLUSTER-wide (the queue is a table, "
                "not an in-process queue), so every replica reports the same values.",
                labels=["status"],
            )
            for status, n in (stats.get("by_status") or {}).items():
                g.add_metric([str(status)[:32]], float(n))
            yield g
        except Exception:
            pass

        # --- per-source resolve health (client telemetry, 14d) ---------------
        try:
            rows = _telemetry_rows()
            ratio = GaugeMetricFamily(
                "crimson_source_success_ratio",
                "Client-reported resolve success ratio per source over 14 days. "
                f"Top {_TELEMETRY_TOP_N} sources by volume only (see _TELEMETRY_TOP_N).",
                labels=["source"],
            )
            events = CounterMetricFamily(
                "crimson_source_resolve_events",
                "Client-reported resolve outcomes per source over 14 days.",
                labels=["source", "outcome"],
            )
            for row in rows:
                source = str(row.get("source") or "")[:80]
                if not source:
                    continue
                rate = row.get("success_rate")
                if rate is not None:
                    ratio.add_metric([source], float(rate))
                events.add_metric([source, "ok"], float(row.get("ok") or 0))
                events.add_metric([source, "fail"], float(row.get("fail") or 0))
            yield ratio
            yield events
        except Exception:
            pass


def install_state_collector() -> None:
    """Register the scrape-time collector. Called once from the lifespan, so an
    import of this module (a test, a script) never touches the DB by itself."""
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        REGISTRY.register(CrimsonStateCollector())
    except Exception as e:  # already registered, or a registry-level clash
        logger.warning(f"metrics state collector not installed: {e}")


def render_metrics() -> Tuple[bytes, str]:
    """The Prometheus text exposition for this replica.

    BLOCKING: the state collector does a database read, so callers must run this
    off the event loop (``run_in_threadpool``) exactly like any other query."""
    if not PROMETHEUS_AVAILABLE:
        return b"", CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def metrics_token() -> str:
    """The shared secret a Prometheus scraper presents. Empty means no token is
    configured, in which case /metrics is reachable by an admin session only."""
    return os.getenv("METRICS_TOKEN", "").strip()
