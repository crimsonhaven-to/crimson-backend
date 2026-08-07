"""
Named-panel Prometheus queries, for the Admin dashboard's Metrics tab.

Phase 0 (``web/routes/metrics.py`` + the client's promParse.js) reads /metrics
straight off whichever replica answered: current values, no history, counters that
reset on every deploy. This module adds the time axis. It talks to a private
Prometheus that scrapes every Swarm task, so the dashboard can draw fleet-wide
rates over hours or days instead of one process's totals since boot.

**The browser never sends PromQL.** It sends a panel id and a range id, both of
which are dictionary keys here; anything not in the dictionary is a 404. That is
deliberate and matches how the rest of this codebase treats client input (see the
signed proxies, the /mw key scoping): a closed vocabulary the server owns, not a
string the server evaluates. Prometheus has no authentication and no notion of a
"read-only" query, so a passthrough would hand any admin session the ability to
run arbitrary expressions against the whole TSDB, including
``/api/v1/admin/tsdb/delete_series`` shaped mischief if the URL were ever
extended carelessly. Keeping the vocabulary closed removes the question entirely.

Two things about the metric set that a naive dashboard gets wrong, both of which
the panels below are written around:

* **Most crimson_* metrics are per replica, so they are summed.** Request
  counters, resolve counters, the pool gauges: each replica reports its own, and
  the fleet number is the sum.
* **Three of them are CLUSTER-wide and must never be summed.**
  ``crimson_download_jobs`` and ``crimson_source_success_ratio`` are read out of
  the shared database, so all N replicas report the same value and a ``sum()``
  would silently multiply it by the replica count. They use ``max()``.
  ``crimson_schema_version`` is per replica but comparing replicas is the entire
  point of it, so it uses ``max()`` and ``min()`` side by side: the two lines
  separating is a rolling deploy caught in the act.

Everything degrades quietly. With ``PROMETHEUS_URL`` unset the endpoints report
``available: false`` and the dashboard keeps showing exactly what it showed in
Phase 0; if Prometheus is set but unreachable, panels return an error string
rather than raising, because a monitoring outage must not take the admin
dashboard down with it.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.http_client import get_http_client

logger = logging.getLogger("crimson.metrics.query")

# The scrape job name in prometheus.yml (deploy/prometheus/prometheus.yml). Every
# panel below filters on it so a Prometheus shared with other projects cannot
# blend foreign timeseries into our charts.
DEFAULT_JOB = "crimson-api"

# Metric/label syntax only. This value is interpolated into PromQL, and although
# it comes from the operator's own environment rather than from a request, it is
# still the one non-literal fragment in an otherwise fully static query, so it is
# filtered rather than trusted.
_JOB_SAFE = re.compile(r"[^A-Za-z0-9_:-]")

# Series per panel. A `by (route)` or `by (source)` panel can fan out further than
# a chart can legibly draw; the widest are already topk() in the query, and this
# is the backstop for the rest. Series are kept by peak value, so the cap drops
# the quiet ones.
MAX_SERIES = 8

_TIMEOUT = float(os.getenv("PROMETHEUS_TIMEOUT", "12") or 12)


def base_url() -> str:
    """The private Prometheus base URL, or "" when the feature is not deployed."""
    raw = os.getenv("PROMETHEUS_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        logger.warning("PROMETHEUS_URL must start with http:// or https://; ignoring %r", raw)
        return ""
    return raw


def job_label() -> str:
    raw = os.getenv("PROMETHEUS_JOB", DEFAULT_JOB).strip() or DEFAULT_JOB
    return _JOB_SAFE.sub("", raw)[:64] or DEFAULT_JOB


def available() -> bool:
    return bool(base_url())


# --- ranges -----------------------------------------------------------------
# step is chosen so every range lands at roughly 240-360 points: enough for the
# chart to look like a line, few enough that the JSON stays small and the SVG
# stays cheap to draw. window is the rate() lookback, always several scrape
# intervals wide so a single missed scrape leaves a dip rather than a hole.


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


# --- panels -----------------------------------------------------------------


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    group: str
    # How the client formats the y axis: rps | seconds | ratio | count | bytes.
    unit: str
    description: str
    # (legend template, PromQL template). $JOB and $WINDOW are substituted here;
    # $label tokens in the legend are substituted per result series from that
    # series' own labels.
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
    # --- Traffic -------------------------------------------------------------
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
    # --- Playback ------------------------------------------------------------
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
    # --- Sources -------------------------------------------------------------
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
    # --- Fleet ---------------------------------------------------------------
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
        "Cache remuxes queued on the cache worker, and admin download jobs by "
        "status. The download queue is a database table shared by every replica, "
        "so it is read with max() and not summed.",
        (
            ("remuxes queued", 'sum(crimson_cache_worker_queue_depth{job="$JOB"})'),
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
    """Every panel, in declaration order (the client groups by `group`)."""
    return [p.as_dict() for p in _PANEL_LIST]


def range_catalogue() -> List[Dict[str, Any]]:
    return [RANGES[key].as_dict() for key in ("1h", "6h", "24h", "7d", "30d")]


# --- legends ----------------------------------------------------------------

_LEGEND_TOKEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def render_legend(template: str, metric: Dict[str, str]) -> str:
    """Fill a legend template from one result series' labels.

    ``"$source"`` against ``{"source": "Jellyfin"}`` is ``"Jellyfin"``. A template
    with no token is a fixed name ("p95"). When a label the template asks for is
    missing, fall back to whatever labels the series does carry rather than
    rendering an empty legend, because an unlabelled line in a chart is useless."""
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


# --- query execution --------------------------------------------------------


def _expand(promql: str, window: str) -> str:
    return promql.replace("$JOB", job_label()).replace("$WINDOW", window)


def _finite(raw: Any) -> Optional[float]:
    """A JSON-safe float, or None for a gap.

    Prometheus sends sample values as STRINGS, and "NaN" / "+Inf" / "-Inf" are all
    legal ones: NaN in particular is what every one of the ratio panels returns
    whenever the denominator was zero (no traffic in that step). Letting those
    through would put a bare NaN token in the response body, which is not valid
    JSON and makes the browser's res.json() throw, blanking the whole tab over a
    quiet minute at 4am. So they become null, which the chart draws as a gap."""
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
    """One Prometheus API call. Raises nothing the caller has to catch beyond the
    httpx errors, which every caller here turns into a reported error string."""
    client = get_http_client()
    response = await client.get(f"{base_url()}{path}", params=params, timeout=_TIMEOUT)
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "success":
        raise ValueError(body.get("error") or "Prometheus rejected the query")
    return body.get("data") or {}


async def query_panel(panel_id: str, range_id: str) -> Dict[str, Any]:
    """Run one named panel over one named range.

    Returns a rendered payload even on failure (``ok: False`` plus an error
    string), because the dashboard shows a dozen panels at once and one upstream
    hiccup should grey out one card rather than break the page."""
    panel = PANELS[panel_id]
    window = RANGES[range_id]

    # Align the window to the step so repeated loads ask Prometheus the exact same
    # question (its query cache can then answer it) and so points from different
    # panels line up on the same x positions in the UI.
    end = math.floor(time.time() / window.step) * window.step
    start = end - window.seconds

    series: List[Dict[str, Any]] = []
    try:
        for legend, promql in panel.series:
            data = await _get(
                "/api/v1/query_range",
                {
                    "query": _expand(promql, window.window),
                    "start": start,
                    "end": end,
                    "step": window.step,
                },
            )
            for result in data.get("result") or []:
                metric = result.get("metric") or {}
                points = [
                    [int(float(ts)), _finite(value)]
                    for ts, value in (result.get("values") or [])
                ]
                if not any(value is not None for _, value in points):
                    # An all-null line is a series Prometheus knows about but has
                    # no data for in this window. Drawing it would add a legend
                    # entry pointing at nothing.
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
    """Which replicas Prometheus is actually scraping, and which it cannot reach.

    Worth surfacing on its own rather than inferring it from the charts: an empty
    chart because nothing happened and an empty chart because the scraper lost the
    fleet look identical, and this is what tells them apart."""
    try:
        data = await _get("/api/v1/targets", {"state": "any"})
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("target listing failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200] or exc.__class__.__name__, "targets": []}

    job = job_label()
    targets = []
    for entry in data.get("activeTargets") or []:
        labels = entry.get("labels") or {}
        if labels.get("job") != job:
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


# Prometheus' retention is a process flag, so it only changes when the operator
# redeploys it. Cached for an hour so opening the tab does not re-ask every time;
# the UI needs it to say "your 30 day range is longer than this server keeps".
_RETENTION: Tuple[float, Optional[str]] = (0.0, None)
_RETENTION_TTL = 3600.0


async def retention_hint() -> Optional[str]:
    """How long this Prometheus keeps data, as its own flag reports it."""
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
