"""Named-panel Prometheus queries for the dashboard's Metrics tab.

/metrics on one replica has no history and resets each deploy; a private
Prometheus scraping every Swarm task gives fleet-wide rates over days.

The browser sends a panel id and a range id, never PromQL. Prometheus has no
authentication and no read-only mode, so a passthrough would let any admin
session run arbitrary expressions against the whole TSDB.

Most crimson_* metrics are per replica and summed. ``crimson_download_jobs`` and
``crimson_source_success_ratio`` come from the shared database, so every replica
reports the same value and they use ``max()``, never ``sum()``.
``crimson_schema_version`` shows ``max()`` and ``min()`` side by side: the lines
separating is a rolling deploy in progress.

A monitoring outage must not take the dashboard down, so failures come back as
an error string in the payload rather than an exception.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.config import get_settings
from core.http_client import http_client

logger = logging.getLogger("crimson.metrics.query")

# The scrape job in deploy/prometheus/prometheus.yml. Every panel filters on it
# so a Prometheus shared with other projects cannot blend in foreign series.
JOB = "crimson-api"

# Backstop for `by (label)` panels wider than a chart can draw; the quietest
# series (by peak) are dropped.
MAX_SERIES = 8

_TIMEOUT = 12.0


def base_url() -> str:
    """The Prometheus base URL, or "" when the feature is not deployed."""
    raw = get_settings().prometheus_url.rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        logger.warning("PROMETHEUS_URL must start with http:// or https://; ignoring %r", raw)
        return ""
    return raw


def available() -> bool:
    return bool(base_url())


# step gives every range roughly 240-360 points: enough for a line, few enough
# to keep the JSON small. window is the rate() lookback, several scrape intervals
# wide so a missed scrape leaves a dip rather than a hole.
@dataclass(frozen=True)
class Range:
    id: str
    label: str
    seconds: int
    step: int
    window: str

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "seconds": self.seconds, "step": self.step}


RANGES: Dict[str, Range] = {
    r.id: r
    for r in (
        Range("1h", "Last hour", 3600, 15, "1m"),
        Range("6h", "Last 6 hours", 21600, 60, "5m"),
        Range("24h", "Last day", 86400, 300, "15m"),
        Range("7d", "Last week", 604800, 1800, "1h"),
        Range("30d", "Last month", 2592000, 7200, "4h"),
    )
}

DEFAULT_RANGE = "6h"


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    group: str
    # How the client formats the y axis: rps | seconds | ratio | count | bytes.
    unit: str
    description: str
    # (legend, PromQL) pairs. $JOB and $WINDOW are expanded in the query; $label
    # tokens in the legend are filled from each result series' labels.
    series: Tuple[Tuple[str, str], ...]
    stacked: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "group": self.group,
            "unit": self.unit,
            "description": self.description,
            "stacked": self.stacked,
        }


_PANEL_LIST: Tuple[Panel, ...] = (
    Panel(
        "http_rate", "Requests per second", "Traffic", "rps",
        "Everything the fleet served, split by status code.",
        (("$status", 'sum by (status) (rate(crimson_http_requests_total{job="$JOB"}[$WINDOW]))'),),
        stacked=True,
    ),
    Panel(
        "http_latency", "Time to response headers", "Traffic", "seconds",
        "Not time to last byte: /watch streams for as long as its slowest scraper "
        "runs, and mixing that in would flatten every percentile. The streaming "
        "side is the Playback panels below.",
        (
            ("p50", 'histogram_quantile(0.5, sum by (le) (rate(crimson_http_request_duration_seconds_bucket{job="$JOB"}[$WINDOW])))'),
            ("p95", 'histogram_quantile(0.95, sum by (le) (rate(crimson_http_request_duration_seconds_bucket{job="$JOB"}[$WINDOW])))'),
            ("p99", 'histogram_quantile(0.99, sum by (le) (rate(crimson_http_request_duration_seconds_bucket{job="$JOB"}[$WINDOW])))'),
        ),
    ),
    Panel(
        "http_errors", "Failed share of requests", "Traffic", "ratio",
        "5xx plus 499 (the client hung up before we answered) over all requests. "
        "Gaps are periods with no traffic at all, not periods with no errors.",
        (
            ("failed", 'sum(rate(crimson_http_requests_total{job="$JOB",status=~"5..|499"}[$WINDOW])) / sum(rate(crimson_http_requests_total{job="$JOB"}[$WINDOW]))'),
        ),
    ),
    Panel(
        "http_routes", "Busiest routes", "Traffic", "rps",
        "By route template, so every episode of every show folds into one line.",
        (("$route", 'topk(6, sum by (route) (rate(crimson_http_requests_total{job="$JOB"}[$WINDOW])))'),),
    ),
    Panel(
        "http_in_flight", "Requests in flight", "Traffic", "count",
        "Concurrent requests across the fleet. A rising floor here while the "
        "request rate is flat means responses are getting slower.",
        (("in flight", 'sum(crimson_http_requests_in_progress{job="$JOB"})'),),
    ),
    Panel(
        "watch_rate", "Watch fan-outs per second", "Playback", "rps",
        "One per /watch request, by how it ended.",
        (("$outcome", 'sum by (outcome) (rate(crimson_watch_requests_total{job="$JOB"}[$WINDOW]))'),),
        stacked=True,
    ),
    Panel(
        "watch_first_stream", "Time to first playable stream", "Playback", "seconds",
        "The number the viewer actually feels: fan-out start until the first "
        "stream is pushed down the NDJSON response.",
        (
            ("p50", 'histogram_quantile(0.5, sum by (le) (rate(crimson_watch_first_stream_seconds_bucket{job="$JOB"}[$WINDOW])))'),
            ("p95", 'histogram_quantile(0.95, sum by (le) (rate(crimson_watch_first_stream_seconds_bucket{job="$JOB"}[$WINDOW])))'),
        ),
    ),
    Panel(
        "watch_yield", "Streams found per fan-out", "Playback", "count",
        "How many playable streams the average fan-out produced. Falling towards "
        "1 means the sources are thinning out even while nothing is erroring.",
        (
            ("streams each", 'sum(rate(crimson_watch_streams_total{job="$JOB"}[$WINDOW])) / sum(rate(crimson_watch_requests_total{job="$JOB"}[$WINDOW]))'),
        ),
    ),
    Panel(
        "resolve_success", "Resolver success rate", "Sources", "ratio",
        "Share of embed resolves that produced a stream, per source. A line "
        "falling off a cliff is an upstream that changed its markup.",
        (
            ("$source", 'sum by (source) (rate(crimson_resolve_total{job="$JOB",outcome="ok"}[$WINDOW])) / sum by (source) (rate(crimson_resolve_total{job="$JOB"}[$WINDOW]))'),
        ),
    ),
    Panel(
        "resolve_rate", "Resolve attempts per second", "Sources", "rps",
        "How hard each source is being worked. Read it next to the success rate: "
        "a dead source with no attempts is not the same problem as a dead source "
        "everyone is still hitting.",
        (("$source", 'sum by (source) (rate(crimson_resolve_total{job="$JOB"}[$WINDOW]))'),),
        stacked=True,
    ),
    Panel(
        "scraper_success", "Scraper hit rate", "Sources", "ratio",
        "Share of discovery runs that came back with embeds.",
        (
            ("$scraper", 'sum by (scraper) (rate(crimson_scraper_runs_total{job="$JOB",outcome="embeds"}[$WINDOW])) / sum by (scraper) (rate(crimson_scraper_runs_total{job="$JOB"}[$WINDOW]))'),
        ),
    ),
    Panel(
        "scraper_latency", "Scraper p95 duration", "Sources", "seconds",
        "Search plus embed discovery, per scraper. The slowest line here sets how "
        "long a fan-out stays open.",
        (
            ("$scraper", 'histogram_quantile(0.95, sum by (le, scraper) (rate(crimson_scraper_duration_seconds_bucket{job="$JOB"}[$WINDOW])))'),
        ),
    ),
    Panel(
        "source_beacons", "Client-reported success rate", "Sources", "ratio",
        "The 14-day aggregate the clients beacon back, read out of the database. "
        "Covers the resolves that happen in the browser and extension, which the "
        "backend counters above never see. Cluster-wide, so it is read with max() "
        "rather than summed across replicas.",
        (("$source", 'max by (source) (crimson_source_success_ratio{job="$JOB"})'),),
    ),
    Panel(
        "replicas", "Replicas being scraped", "Fleet", "count",
        "Straight off Prometheus' own up series, so this counts what the scraper "
        "can reach rather than what Swarm believes it scheduled.",
        (
            ("reachable", 'count(up{job="$JOB"} == 1)'),
            ("unreachable", 'count(up{job="$JOB"} == 0)'),
        ),
    ),
    Panel(
        "memory", "Resident memory", "Fleet", "bytes",
        "Fleet total and the single hungriest replica. The stack limits a serving "
        "replica to 768M, so watch the peak line rather than the total.",
        (
            ("fleet total", 'sum(process_resident_memory_bytes{job="$JOB"})'),
            ("busiest replica", 'max(process_resident_memory_bytes{job="$JOB"})'),
        ),
    ),
    Panel(
        "cpu", "CPU cores in use", "Fleet", "count",
        "Summed CPU seconds per second, which reads directly as cores.",
        (("cores", 'sum(rate(process_cpu_seconds_total{job="$JOB"}[$WINDOW]))'),),
    ),
    Panel(
        "db_pool", "Database connections", "Fleet", "count",
        "Checked-out connections against the fleet's configured ceiling. If the "
        "in-use line reaches the ceiling, requests start queueing on the pool.",
        (
            ("in use", 'sum(crimson_db_pool_in_use{job="$JOB"})'),
            ("ceiling", 'sum(crimson_db_pool_max_size{job="$JOB"})'),
            ("waiting", 'sum(crimson_db_pool_waiting{job="$JOB"})'),
        ),
    ),
    Panel(
        "cache_hit", "Response cache hit rate", "Fleet", "ratio",
        "Per tier: l1 is in-process and per replica, l2 is the shared Postgres "
        "tier every replica sees.",
        (
            ("$tier", 'sum by (tier) (rate(crimson_response_cache_total{job="$JOB",result="hit"}[$WINDOW])) / sum by (tier) (rate(crimson_response_cache_total{job="$JOB"}[$WINDOW]))'),
        ),
    ),
    Panel(
        "workers", "Background worker backlog", "Fleet", "count",
        "Cache remuxes running on the cache worker, and admin download jobs by "
        "status. The download queue is a database table shared by every replica, "
        "so it is read with max() and not summed.",
        (
            ("remuxes running", 'sum(crimson_cache_worker_inflight{job="$JOB"})'),
            ("downloads $status", 'max by (status) (crimson_download_jobs{job="$JOB"})'),
        ),
    ),
    Panel(
        "schema", "Schema version across replicas", "Fleet", "count",
        "The highest and lowest migration version any replica booted at. The two "
        "lines separate during a rolling deploy and must converge when it "
        "finishes; if they stay apart, a replica is stuck on the old image.",
        (
            ("highest", 'max(crimson_schema_version{job="$JOB"})'),
            ("lowest", 'min(crimson_schema_version{job="$JOB"})'),
        ),
    ),
)

PANELS: Dict[str, Panel] = {p.id: p for p in _PANEL_LIST}


def panel_catalogue() -> List[Dict[str, Any]]:
    return [p.as_dict() for p in _PANEL_LIST]


def range_catalogue() -> List[Dict[str, Any]]:
    return [r.as_dict() for r in RANGES.values()]

_LEGEND_TOKEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def render_legend(template: str, metric: Dict[str, str]) -> str:
    """Fill a legend template from one series' labels. If a label is missing, fall
    back to whatever labels the series does carry: an unnamed line is useless."""
    if "$" not in template:
        return template

    missing: List[str] = []

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        value = metric.get(key)
        if value is None:
            missing.append(key)
            return ""
        return str(value)

    rendered = _LEGEND_TOKEN.sub(_sub, template).strip()
    if rendered and not missing:
        return rendered

    leftovers = {k: v for k, v in metric.items() if k not in ("__name__", "job")}
    if leftovers:
        return ", ".join(f"{v}" for _, v in sorted(leftovers.items()))
    return rendered or template


def _expand(promql: str, window: str) -> str:
    return promql.replace("$JOB", JOB).replace("$WINDOW", window)


def _finite(raw: Any) -> Optional[float]:
    """A JSON-safe float, or None for a gap.

    Ratio panels return "NaN" whenever a step had no traffic. A bare NaN in the
    body is invalid JSON and makes the browser's res.json() throw, blanking the
    tab; null draws as a gap instead."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _peak(points: List[List[Any]]) -> float:
    best = 0.0
    for _, value in points:
        if value is not None and value > best:
            best = value
    return best


async def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One Prometheus API call. Raises httpx.HTTPError or ValueError."""
    async with http_client() as client:
        response = await client.get(f"{base_url()}{path}", params=params, timeout=_TIMEOUT)
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "success":
        raise ValueError(body.get("error") or "Prometheus rejected the query")
    return body.get("data") or {}


async def query_panel(panel_id: str, range_id: str) -> Dict[str, Any]:
    """Run one named panel over one named range. Failures come back as
    ``ok: False`` with an error string, so one hiccup greys out a single card
    rather than breaking a page of a dozen."""
    panel = PANELS.get(panel_id)
    window = RANGES.get(range_id)
    if panel is None or window is None:
        return {"ok": False, "error": "unknown panel or range", "series": []}

    # Aligned to the step so repeated loads ask the identical question, which
    # Prometheus can answer from cache, and panels share x positions.
    end = math.floor(time.time() / window.step) * window.step
    start = end - window.seconds

    series: List[Dict[str, Any]] = []
    try:
        responses = await asyncio.gather(*(
            _get(
                "/api/v1/query_range",
                {
                    "query": _expand(promql, window.window),
                    "start": start,
                    "end": end,
                    "step": window.step,
                },
            )
            for _, promql in panel.series
        ))
        for (legend, _), data in zip(panel.series, responses):
            for result in data.get("result") or []:
                metric = result.get("metric") or {}
                points = [
                    [int(float(ts)), _finite(value)]
                    for ts, value in (result.get("values") or [])
                ]
                # Known to Prometheus but empty in this window: drawing it would
                # add a legend entry pointing at nothing.
                if not any(value is not None for _, value in points):
                    continue
                series.append({"label": render_legend(legend, metric), "points": points})
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("panel %s over %s failed: %s", panel_id, range_id, exc)
        return {
            "ok": False,
            "panel": panel.as_dict(),
            "range": window.as_dict(),
            "error": str(exc)[:200] or exc.__class__.__name__,
            "series": [],
        }

    truncated = 0
    if len(series) > MAX_SERIES:
        series.sort(key=lambda s: _peak(s["points"]), reverse=True)
        truncated = len(series) - MAX_SERIES
        series = series[:MAX_SERIES]

    return {
        "ok": True,
        "panel": panel.as_dict(),
        "range": window.as_dict(),
        "start": start,
        "end": end,
        "step": window.step,
        "series": series,
        "truncated": truncated,
    }


async def scrape_targets() -> Dict[str, Any]:
    """Which replicas Prometheus is scraping and which it cannot reach. An empty
    chart from no traffic and one from a scraper that lost the fleet look
    identical, so this is shown separately."""
    try:
        data = await _get("/api/v1/targets", {"state": "any"})
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("target listing failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200] or exc.__class__.__name__, "targets": []}

    targets = []
    for entry in data.get("activeTargets") or []:
        labels = entry.get("labels") or {}
        if labels.get("job") != JOB:
            continue
        targets.append({
            "instance": labels.get("instance") or "?",
            "health": entry.get("health") or "unknown",
            "last_scrape": entry.get("lastScrape"),
            "last_error": (entry.get("lastError") or "")[:200] or None,
        })
    targets.sort(key=lambda t: t["instance"])
    return {
        "ok": True,
        "targets": targets,
        "up": sum(1 for t in targets if t["health"] == "up"),
        "down": sum(1 for t in targets if t["health"] != "up"),
    }


# Retention is a Prometheus process flag that only changes on redeploy. The UI
# uses it to warn when a range is longer than the server keeps.
_RETENTION: Tuple[float, Optional[str]] = (0.0, None)
_RETENTION_TTL = 3600.0


async def retention_hint() -> Optional[str]:
    global _RETENTION
    cached_at, value = _RETENTION
    if value is not None and time.monotonic() - cached_at < _RETENTION_TTL:
        return value
    try:
        data = await _get("/api/v1/status/flags")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    found = data.get("storage.tsdb.retention.time") or data.get("storage.tsdb.retention")
    if not found or found in ("0s", "0"):
        return None
    _RETENTION = (time.monotonic(), str(found))
    return str(found)
