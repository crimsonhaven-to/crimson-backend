"""Tests for the named-panel Prometheus layer (core/prom_query.py).

No network and no Prometheus: the HTTP call is stubbed, so what is under test is
the part that actually rots. Three kinds of breakage are covered.

1. **The panel catalogue is a data structure that is easy to typo.** A stray
   ``$JOB`` left unexpanded, a legend token that no query can ever produce, or a
   ``sum()`` over one of the three cluster-wide metrics all produce a chart that
   looks plausible and is wrong. Those are asserted structurally rather than by
   eyeballing the file.
2. **NaN is the normal case, not the edge case.** Every ratio panel divides by a
   rate that is zero whenever traffic is zero, and Prometheus reports that as the
   string "NaN". Letting it through emits invalid JSON and blanks the whole tab.
3. **The trust boundary.** The browser sends a dictionary key, never an
   expression.
"""

import json

import httpx
import pytest

from core import prom_query


# --- catalogue integrity ----------------------------------------------------


def test_every_panel_has_a_unique_id_and_a_known_unit():
    ids = [p.id for p in prom_query.PANELS.values()]
    assert len(ids) == len(set(ids))
    for panel in prom_query.PANELS.values():
        assert panel.unit in ("rps", "seconds", "ratio", "count", "bytes"), panel.id
        assert panel.group, panel.id
        assert panel.series, panel.id


def test_no_panel_leaves_a_placeholder_unexpanded(monkeypatch):
    """$JOB / $WINDOW are substituted by _expand. One that survives expansion is a
    typo (say `$job`) that Prometheus would reject at query time, which shows up
    as a permanently broken card rather than a crash."""
    monkeypatch.delenv("PROMETHEUS_JOB", raising=False)
    for panel in prom_query.PANELS.values():
        for _, promql in panel.series:
            expanded = prom_query._expand(promql, "5m")
            assert "$" not in expanded, f"{panel.id}: {expanded}"
            assert "crimson-api" in expanded or "up{" in expanded or "process_" in expanded


def test_cluster_wide_metrics_are_never_summed():
    """crimson_download_jobs and crimson_source_success_ratio are read out of the
    shared database, so EVERY replica exports the same value. sum() over them
    multiplies the truth by the replica count, and the result looks entirely
    believable, which is what makes it dangerous."""
    cluster_wide = ("crimson_download_jobs", "crimson_source_success_ratio")
    for panel in prom_query.PANELS.values():
        for _, promql in panel.series:
            for metric in cluster_wide:
                if metric in promql:
                    assert "sum" not in promql.split(metric)[0], f"{panel.id} sums {metric}"


def test_every_panel_filters_on_the_scrape_job(monkeypatch):
    """A Prometheus shared with another project must not blend foreign series into
    our charts."""
    monkeypatch.delenv("PROMETHEUS_JOB", raising=False)
    for panel in prom_query.PANELS.values():
        for _, promql in panel.series:
            assert 'job="$JOB"' in promql, f"{panel.id} does not scope to the job"


def test_ranges_produce_a_drawable_number_of_points():
    for window in prom_query.RANGES.values():
        points = window.seconds / window.step
        assert 100 <= points <= 600, f"{window.id} would return {points:.0f} points"


def test_range_catalogue_lists_every_range():
    assert {r["id"] for r in prom_query.range_catalogue()} == set(prom_query.RANGES)


# --- availability -----------------------------------------------------------


def test_unset_prometheus_url_is_unavailable_not_an_error(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    assert prom_query.available() is False
    assert prom_query.base_url() == ""


def test_a_url_without_a_scheme_is_refused(monkeypatch):
    """Reported as "not configured" rather than half-working: the module builds
    URLs by concatenation, and a bare host would produce nonsense requests."""
    monkeypatch.setenv("PROMETHEUS_URL", "prometheus:9090")
    assert prom_query.available() is False


def test_trailing_slash_is_stripped(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090/")
    assert prom_query.base_url() == "http://prometheus:9090"


def test_job_label_is_filtered_before_it_reaches_promql(monkeypatch):
    """The job name is operator-supplied rather than client-supplied, so this is
    defence in depth, not the main gate. It is still the only non-literal fragment
    in an otherwise static query."""
    monkeypatch.setenv("PROMETHEUS_JOB", 'evil"} or up{x="')
    assert '"' not in prom_query.job_label()
    assert prom_query.job_label() == "evilorupx"


def test_a_blank_job_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_JOB", "   ")
    assert prom_query.job_label() == prom_query.DEFAULT_JOB


# --- legends ----------------------------------------------------------------


def test_legend_without_a_token_is_a_fixed_name():
    assert prom_query.render_legend("p95", {"le": "0.5"}) == "p95"


def test_legend_token_is_filled_from_the_series_labels():
    assert prom_query.render_legend("$source", {"source": "Jellyfin"}) == "Jellyfin"
    assert prom_query.render_legend("downloads $status", {"status": "queued"}) == "downloads queued"


def test_legend_falls_back_to_the_labels_when_the_token_is_missing():
    """An unlabelled line in a chart is useless, so a template asking for a label
    the series does not carry must still name the line somehow."""
    out = prom_query.render_legend("$source", {"scraper": "AniWorld", "job": "crimson-api"})
    assert out == "AniWorld"
    assert "job" not in out


def test_legend_with_nothing_to_go_on_keeps_the_template():
    assert prom_query.render_legend("$source", {}) == "$source"


# --- value normalisation ----------------------------------------------------


@pytest.mark.parametrize("raw", ["NaN", "+Inf", "-Inf", None, "", "not-a-number"])
def test_non_finite_samples_become_gaps(raw):
    assert prom_query._finite(raw) is None


def test_ordinary_samples_survive():
    assert prom_query._finite("0.5") == 0.5
    assert prom_query._finite("1.7860921864795718e+09") == pytest.approx(1786092186.5, rel=1e-6)


# --- query execution --------------------------------------------------------


class _StubClient:
    """Stands in for the shared httpx.AsyncClient. Records what was asked for so
    the query construction can be asserted, not just the response handling."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        return httpx.Response(
            self.status,
            content=json.dumps(self.payload).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", url),
        )


def _matrix(*series):
    return {"status": "success", "data": {"resultType": "matrix", "result": list(series)}}


@pytest.fixture
def prom(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")
    monkeypatch.delenv("PROMETHEUS_JOB", raising=False)

    def _install(payload, status=200):
        client = _StubClient(payload, status)
        monkeypatch.setattr(prom_query, "get_http_client", lambda: client)
        return client

    return _install


async def test_query_panel_shapes_a_matrix_into_labelled_series(prom):
    client = prom(_matrix(
        {"metric": {"source": "Jellyfin"}, "values": [[1000, "0.9"], [1015, "1"]]},
        {"metric": {"source": "Local Media"}, "values": [[1000, "0.5"], [1015, "0.6"]]},
    ))

    out = await prom_query.query_panel("resolve_success", "1h")

    assert out["ok"] is True
    assert [s["label"] for s in out["series"]] == ["Jellyfin", "Local Media"]
    assert out["series"][0]["points"] == [[1000, 0.9], [1015, 1.0]]
    assert out["step"] == 15
    assert out["end"] - out["start"] == 3600
    # One query per declared series expression, against query_range.
    assert len(client.calls) == 1
    url, params = client.calls[0]
    assert url == "http://prometheus:9090/api/v1/query_range"
    assert "$" not in params["query"]
    assert params["step"] == 15


async def test_the_query_window_is_aligned_to_the_step(prom):
    """So repeated loads ask the identical question (Prometheus can then serve it
    from cache) and points from different panels land on the same x positions."""
    prom(_matrix())
    out = await prom_query.query_panel("http_rate", "24h")
    assert out["end"] % out["step"] == 0
    assert out["start"] % out["step"] == 0


async def test_nan_samples_become_nulls_and_json_stays_valid(prom):
    """The ratio panels divide by a rate that is zero whenever traffic is zero.
    Prometheus reports that as "NaN"; passing it through produces a body Python
    will happily emit and the browser's res.json() will refuse."""
    prom(_matrix({"metric": {}, "values": [[1000, "NaN"], [1015, "0.25"], [1030, "NaN"]]}))

    out = await prom_query.query_panel("http_errors", "1h")

    assert out["series"][0]["points"] == [[1000, None], [1015, 0.25], [1030, None]]
    body = json.dumps(out)
    assert "NaN" not in body and "Infinity" not in body


async def test_an_all_null_series_is_dropped(prom):
    """Prometheus knows the series exists but has nothing in this window. Drawing
    it would add a legend entry pointing at an empty line."""
    prom(_matrix(
        {"metric": {"source": "Ghost"}, "values": [[1000, "NaN"], [1015, "NaN"]]},
        {"metric": {"source": "Real"}, "values": [[1000, "1"]]},
    ))
    out = await prom_query.query_panel("resolve_success", "1h")
    assert [s["label"] for s in out["series"]] == ["Real"]


async def test_series_are_capped_keeping_the_biggest(prom, monkeypatch):
    monkeypatch.setattr(prom_query, "MAX_SERIES", 3)
    prom(_matrix(*[
        {"metric": {"source": f"s{i}"}, "values": [[1000, str(i)]]}
        for i in range(6)
    ]))

    out = await prom_query.query_panel("resolve_rate", "1h")

    assert len(out["series"]) == 3
    assert out["truncated"] == 3
    assert [s["label"] for s in out["series"]] == ["s5", "s4", "s3"]


async def test_a_prometheus_outage_greys_out_one_panel_rather_than_raising(prom):
    """A dozen panels load at once. One upstream hiccup must not take the tab down
    with it, so failures are reported in the payload, never thrown."""
    prom({"status": "error", "error": "expanding series: out of memory"}, status=200)

    out = await prom_query.query_panel("http_rate", "6h")

    assert out["ok"] is False
    assert "out of memory" in out["error"]
    assert out["series"] == []
    assert out["panel"]["id"] == "http_rate"


async def test_an_http_error_is_reported_not_raised(prom):
    prom({"status": "error"}, status=502)
    out = await prom_query.query_panel("http_rate", "6h")
    assert out["ok"] is False
    assert out["series"] == []


async def test_scrape_targets_reports_only_our_job(prom):
    prom({
        "status": "success",
        "data": {
            "activeTargets": [
                {"labels": {"job": "crimson-api", "instance": "10.0.1.9:8000"},
                 "health": "up", "lastScrape": "2026-08-07T10:00:00Z", "lastError": ""},
                {"labels": {"job": "crimson-api", "instance": "10.0.1.3:8000"},
                 "health": "down", "lastError": "server returned HTTP status 401 Unauthorized"},
                {"labels": {"job": "prometheus", "instance": "127.0.0.1:9090"}, "health": "up"},
            ]
        },
    })

    out = await prom_query.scrape_targets()

    assert out["ok"] is True
    assert out["up"] == 1 and out["down"] == 1
    assert [t["instance"] for t in out["targets"]] == ["10.0.1.3:8000", "10.0.1.9:8000"]
    assert "401" in out["targets"][0]["last_error"]
    # An empty lastError is None, not "", so the client can test it directly.
    assert out["targets"][1]["last_error"] is None


async def test_retention_hint_reads_the_server_flag(prom, monkeypatch):
    monkeypatch.setattr(prom_query, "_RETENTION", (0.0, None))
    prom({"status": "success", "data": {"storage.tsdb.retention.time": "45d"}})
    assert await prom_query.retention_hint() == "45d"


async def test_retention_hint_ignores_the_zero_default(prom, monkeypatch):
    """Prometheus reports 0s when no explicit retention is configured, which means
    "the built-in default", not "keeps nothing"."""
    monkeypatch.setattr(prom_query, "_RETENTION", (0.0, None))
    prom({"status": "success", "data": {"storage.tsdb.retention.time": "0s"}})
    assert await prom_query.retention_hint() is None
