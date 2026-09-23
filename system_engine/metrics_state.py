"""Live subsystem state, read at scrape time.

Reading values where they already live, rather than polling on a timer, needs
no background job and is exactly as fresh as the scrape. Each section is
guarded on its own, so a database outage costs the DB gauges, not a 500 that
blinds the operator exactly when it matters.
"""

import logging
import time
from typing import Tuple

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from cache_engine.downloader import manager as cache_manager
from core import migrations
from core import db_pool
from core.metrics import REGISTRY
from core.version import VERSION
from download_engine.manager import manager as download_manager
from telemetry_engine.db import store as telemetry_store

logger = logging.getLogger("crimson.metrics.state")

# Source names in the telemetry table come from a client beacon, so they have no
# fixed vocabulary; the cap keeps a hostile client from minting endless series.
# Rows come busiest first, so the real sources survive the cut.
TELEMETRY_TOP_N = 25
# The one database read here; scrapes come far faster than daily aggregates move.
_TELEMETRY_TTL = 60.0
_telemetry_cache: Tuple[float, list] = (0.0, [])

_POOL_GAUGES = (
    ("size", "Connections currently held by the pool."),
    ("idle", "Pooled connections available right now."),
    ("in_use", "Pooled connections currently checked out."),
    ("waiting", "Requests blocked waiting for a connection."),
    ("max_size", "Configured pool ceiling."),
    ("min_size", "Configured pool floor."),
)
_POOL_COUNTERS = (
    ("requests_total", "crimson_db_pool_requests_total", "Connection requests served by the pool."),
    ("requests_errors", "crimson_db_pool_request_errors_total", "Connection requests that failed."),
    ("connections_total", "crimson_db_pool_connections_total", "Connections opened by the pool."),
)


def telemetry_rows() -> list:
    global _telemetry_cache
    cached_at, rows = _telemetry_cache
    now = time.monotonic()
    if rows and now - cached_at < _TELEMETRY_TTL:
        return rows
    rows = telemetry_store.top_stats(days=14)[:TELEMETRY_TOP_N]
    _telemetry_cache = (now, rows)
    return rows


def _gauge(name: str, doc: str, value: float) -> GaugeMetricFamily:
    g = GaugeMetricFamily(name, doc)
    g.add_metric([], float(value))
    return g


def _build_and_schema():
    info = GaugeMetricFamily(
        "crimson_build_info", "Always 1; the labels carry the running version.", labels=["version"]
    )
    info.add_metric([str(VERSION)], 1)
    yield info
    schema = migrations.cached_status() or {}
    if schema.get("version") is not None:
        yield _gauge(
            "crimson_schema_version",
            "Schema migration version this replica booted at. A rolling deploy that "
            "leaves replicas disagreeing shows up here.",
            schema["version"],
        )
    yield _gauge(
        "crimson_schema_drift",
        "Migration files whose checksum no longer matches what was applied.",
        len(schema.get("drift") or []),
    )


def _pool():
    stats = db_pool.pool_stats()
    if not stats.get("available"):
        return
    for key, doc in _POOL_GAUGES:
        if stats.get(key) is not None:
            yield _gauge(f"crimson_db_pool_{key}", doc, stats[key])
    for key, name, doc in _POOL_COUNTERS:
        if stats.get(key) is not None:
            c = CounterMetricFamily(name, doc)
            c.add_metric([], float(stats[key]))
            yield c


def _cache_worker():
    # Only the cache-worker replica runs one; elsewhere 0 is the correct answer.
    cache = cache_manager.worker_stats()
    yield _gauge(
        "crimson_cache_worker_inflight",
        "Remux jobs currently running in this replica's cache worker.",
        cache.get("inflight", 0),
    )


def _download_jobs():
    jobs = GaugeMetricFamily(
        "crimson_download_jobs",
        "Admin download jobs by status. CLUSTER-wide (the queue is a table, not an "
        "in-process queue), so every replica reports the same values.",
        labels=["status"],
    )
    for status, n in (download_manager.worker_stats().get("by_status") or {}).items():
        jobs.add_metric([str(status)[:32]], float(n))
    yield jobs


def _source_health():
    ratio = GaugeMetricFamily(
        "crimson_source_success_ratio",
        f"Client-reported resolve success ratio per source over 14 days. Top {TELEMETRY_TOP_N} sources by volume only.",
        labels=["source"],
    )
    events = CounterMetricFamily(
        "crimson_source_resolve_events",
        "Client-reported resolve outcomes per source over 14 days.",
        labels=["source", "outcome"],
    )
    for row in telemetry_rows():
        source = str(row.get("source") or "")[:80]
        if not source:
            continue
        if row.get("success_rate") is not None:
            ratio.add_metric([source], float(row["success_rate"]))
        events.add_metric([source, "ok"], float(row.get("ok") or 0))
        events.add_metric([source, "fail"], float(row.get("fail") or 0))
    yield ratio
    yield events


class StateCollector:
    SECTIONS = (_build_and_schema, _pool, _cache_worker, _download_jobs, _source_health)

    def collect(self):
        for section in self.SECTIONS:
            try:
                yield from list(section())
            except Exception as e:
                logger.debug(f"metrics section {section.__name__} skipped: {e}")


def install() -> None:
    """Called from the lifespan, so importing the app never touches the database."""
    try:
        REGISTRY.register(StateCollector())
    except ValueError as e:
        logger.warning(f"metrics state collector not installed: {e}")
