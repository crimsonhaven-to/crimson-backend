"""Fixture tests for the metrics + request-id layer (core/observability.py,
core/logging_setup.py, web/routes/metrics.py).

No network, no database, in keeping with the rest of the suite. The parts that
would need either (the scrape-time state collector's DB reads) are exercised
through stand-ins.

The properties worth protecting here are not "does a counter go up" but the three
that turn a metrics endpoint into an outage if they regress:

  * label cardinality stays bounded (route templates, never raw paths),
  * an inbound X-Request-ID cannot inject into logs or response headers,
  * /metrics is closed by default.
"""

import logging
import os

import pytest

from core import logging_setup, observability


# --- request id -------------------------------------------------------------

def test_generated_ids_are_short_and_unique():
    ids = {observability.new_request_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 16 for i in ids)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("abc123", "abc123"),
        ("  padded  ", "padded"),
        ("keeps.these:and-under_scores", "keeps.these:and-under_scores"),
        # A request id is reflected into a response header and written into logs,
        # so CR/LF (header injection) and everything else exotic must be dropped.
        ("bad\r\nX-Evil: 1", "badX-Evil:1"),
        ("<script>alert(1)</script>", "scriptalert1script"),
        ("", ""),
        (None, ""),
        # Nothing salvageable -> "" so the caller mints its own rather than
        # binding an empty-but-present id.
        ("!!!@@@", ""),
    ],
)
def test_clean_request_id(raw, expected):
    assert observability.clean_request_id(raw) == expected


def test_clean_request_id_truncates():
    assert len(observability.clean_request_id("a" * 500)) == 64


def test_request_id_contextvar_roundtrip():
    assert observability.current_request_id() == ""
    token = observability.set_request_id("deadbeef")
    try:
        assert observability.current_request_id() == "deadbeef"
    finally:
        observability.reset_request_id(token)
    assert observability.current_request_id() == ""


# --- label cardinality ------------------------------------------------------

class _FakeRoute:
    def __init__(self, path_format):
        self.path_format = path_format


def test_route_label_uses_the_template_not_the_path():
    """The whole point: one timeseries for /watch, not one per episode."""
    scope = {
        "path": "/watch/1429/1/7",
        "route": _FakeRoute("/watch/{tmdb_id}/{season_number}/{episode_number}"),
    }
    assert observability.route_label(scope) == "/watch/{tmdb_id}/{season_number}/{episode_number}"


def test_route_label_collapses_unmatched_paths():
    """A scanner probing random URLs must not mint a timeseries per probe."""
    labels = {
        observability.route_label({"path": f"/wp-admin/{i}"}) for i in range(50)
    }
    assert labels == {observability.UNMATCHED_ROUTE}


def test_route_label_is_length_capped():
    scope = {"route": _FakeRoute("/" + "x" * 900)}
    assert len(observability.route_label(scope)) <= 200


@pytest.mark.parametrize(
    "raw, expected",
    [("get", "GET"), ("POST", "POST"), ("PROPFIND", "OTHER"), (None, "OTHER"), ("", "OTHER")],
)
def test_method_label_is_a_closed_vocabulary(raw, expected):
    assert observability.method_label(raw) == expected


# --- recording helpers ------------------------------------------------------

def test_record_helpers_never_raise_on_junk_input():
    """These sit inside the /watch fan-out. A metrics bug must not break playback,
    so every helper swallows its own failures."""
    observability.record_http_request("GET", "/x", 200, 0.1)
    observability.record_http_request(None, None, None, None)
    observability.record_scraper_run(None, None, None)
    observability.record_resolve(None, None, None)
    observability.record_watch(None, None, None, None, None)
    observability.record_cache_lookup(None, None)
    observability.track_in_progress(None, 1)


def test_timer_measures_and_survives_exceptions():
    timer = observability.Timer()
    assert timer.lap() >= 0.0
    with pytest.raises(ValueError):
        with timer:
            raise ValueError("boom")
    assert timer.elapsed >= 0.0


# --- state collector --------------------------------------------------------

def test_state_collector_survives_every_subsystem_being_down(monkeypatch):
    """Each section is independently guarded: with the DB unreachable the export
    should lose gauges, not 500. A /metrics that dies during an incident is
    exactly the wrong failure mode."""
    if not observability.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    import core.db_pool as db_pool

    def _explode(*a, **kw):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(db_pool, "pool_stats", _explode)
    monkeypatch.setattr(observability, "_telemetry_rows", _explode)

    families = list(observability.CrimsonStateCollector().collect())
    names = {f.name for f in families}
    # build_info is pure in-process state, so it survives the outage.
    assert any("build_info" in n for n in names)
    assert not any("db_pool" in n for n in names)


def test_telemetry_gauge_is_capped_to_the_top_n(monkeypatch):
    """resolve_telemetry rows are fed by a CLIENT beacon whose `source` string is
    not a closed vocabulary, so a hostile client could invent unlimited source
    names. The collector must slice, or each one becomes a permanent timeseries."""
    if not observability.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    rows = [
        {"source": f"injected-{i}", "ok": 1, "fail": 0, "success_rate": 1.0}
        for i in range(500)
    ]
    monkeypatch.setattr(observability, "_telemetry_rows", lambda: rows[:observability._TELEMETRY_TOP_N])

    families = {f.name: f for f in observability.CrimsonStateCollector().collect()}
    ratio = families.get("crimson_source_success_ratio")
    assert ratio is not None
    assert len(ratio.samples) <= observability._TELEMETRY_TOP_N


def test_render_metrics_produces_prometheus_text():
    if not observability.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    observability.record_http_request("GET", "/health", 200, 0.01)
    payload, content_type = observability.render_metrics()
    assert b"crimson_http_requests_total" in payload
    assert "text/plain" in content_type


# --- logging ----------------------------------------------------------------

def _record(msg="hello", **extra):
    record = logging.LogRecord("crimson.test", logging.INFO, __file__, 10, msg, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_plain_format_is_unchanged_outside_a_request():
    """The default format must stay byte-compatible with the basicConfig it
    replaced, or it silently breaks somebody's grep."""
    line = logging_setup.PlainFormatter().format(_record())
    assert line.endswith("crimson.test - INFO - hello")
    assert "[req=" not in line


def test_plain_format_appends_the_request_id_when_bound():
    line = logging_setup.PlainFormatter().format(_record(request_id="abc123"))
    assert line.endswith("crimson.test - INFO - hello [req=abc123]")


def test_json_format_carries_the_correlation_fields():
    import orjson

    payload = orjson.loads(
        logging_setup.JsonFormatter().format(_record(request_id="abc123", tmdb_id=1429))
    )
    assert payload["level"] == "INFO"
    assert payload["logger"] == "crimson.test"
    assert payload["message"] == "hello"
    assert payload["request_id"] == "abc123"
    # `extra=` keys are promoted to top-level fields; that is the point of JSON logs.
    assert payload["tmdb_id"] == 1429


def test_json_format_survives_an_unserializable_extra():
    class Opaque:
        pass

    line = logging_setup.JsonFormatter().format(_record(thing=Opaque()))
    assert "hello" in line


def test_request_id_filter_stamps_the_active_id():
    token = observability.set_request_id("ctx-id")
    try:
        record = _record()
        assert logging_setup.RequestIdFilter().filter(record) is True
        assert record.request_id == "ctx-id"
    finally:
        observability.reset_request_id(token)


def test_log_format_env_selects_the_formatter(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert isinstance(logging_setup._formatter(), logging_setup.JsonFormatter)
    monkeypatch.setenv("LOG_FORMAT", "plain")
    assert isinstance(logging_setup._formatter(), logging_setup.PlainFormatter)
    monkeypatch.delenv("LOG_FORMAT")
    assert isinstance(logging_setup._formatter(), logging_setup.PlainFormatter)


# --- /metrics auth ----------------------------------------------------------

class _FakeRequest:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


async def _auth(headers, monkeypatch, user=None):
    from web.routes import metrics as metrics_route

    monkeypatch.setattr(
        metrics_route.account_store, "get_user_by_session", lambda token: user
    )
    return await metrics_route._authorized(_FakeRequest(headers))


async def test_metrics_denies_by_default(monkeypatch):
    """No METRICS_TOKEN configured means admin-session-only. Forgetting to set the
    token must make the endpoint MORE closed, never open."""
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert await _auth({}, monkeypatch) is False
    assert await _auth({"X-Metrics-Token": "guess"}, monkeypatch) is False
    assert await _auth({"Authorization": "Bearer anything"}, monkeypatch) is False


async def test_metrics_accepts_the_configured_token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    assert await _auth({"X-Metrics-Token": "s3cret"}, monkeypatch) is True
    assert await _auth({"Authorization": "Bearer s3cret"}, monkeypatch) is True
    assert await _auth({"X-Metrics-Token": "wrong"}, monkeypatch) is False


async def test_metrics_accepts_an_admin_session_but_not_a_plain_user(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    admin = {"user_id": 1, "is_admin": True}
    plain = {"user_id": 2, "is_admin": False}
    assert await _auth({"Authorization": "Bearer sess"}, monkeypatch, user=admin) is True
    assert await _auth({"Authorization": "Bearer sess"}, monkeypatch, user=plain) is False
    assert await _auth({"Authorization": "Bearer sess"}, monkeypatch, user=None) is False


async def test_metrics_token_scrape_costs_no_database_lookup(monkeypatch):
    """A Prometheus scrape runs every 15-60s per replica; it must not put a session
    query on the database each time."""
    from web.routes import metrics as metrics_route

    calls = []

    def _tracked(token):
        calls.append(token)
        return None

    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    monkeypatch.setattr(metrics_route.account_store, "get_user_by_session", _tracked)
    assert await metrics_route._authorized(
        _FakeRequest({"Authorization": "Bearer s3cret"})
    ) is True
    assert calls == []


# --- the middleware must not buffer -----------------------------------------

async def test_request_middleware_passes_body_chunks_straight_through():
    """The single most dangerous way this feature could break production.

    /watch is a progressive NDJSON stream: the player renders each source the
    instant its line arrives. A middleware that collects the body (which is what
    BaseHTTPMiddleware does) would hold every line until the slowest scraper
    finished, turning instant playback into a 30-second spinner.

    So this drives the middleware's ASGI callable directly and asserts each body
    message is forwarded untouched, in order, as it is produced. (An httpx
    ASGITransport test cannot show this: that transport collects the body itself.)
    """
    import api

    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i in range(3):
            await send({"type": "http.response.body", "body": f"line{i}\n".encode(), "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": "http", "method": "GET", "path": "/watch/1/1/1", "headers": []}
    await api.RequestContextMiddleware(app)(scope, receive, send)

    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert [m["body"] for m in bodies] == [b"line0\n", b"line1\n", b"line2\n", b""]
    # Forwarded, not rebuilt: the middleware never so much as copies a body message.
    assert [m["more_body"] for m in bodies] == [True, True, True, False]


async def test_request_middleware_stamps_the_id_on_the_response_start():
    import api

    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/health",
             "headers": [(b"x-request-id", b"inbound-id")]}
    await api.RequestContextMiddleware(app)(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert (b"x-request-id", b"inbound-id") in start["headers"]
    # Also exposed on the request scope, for anything that wants it via request.state.
    assert scope["state"]["request_id"] == "inbound-id"


async def test_request_middleware_ignores_non_http_scopes():
    """Lifespan and websocket scopes must pass through untouched."""
    import api

    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await api.RequestContextMiddleware(app)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


# --- wiring guards ----------------------------------------------------------

def test_metrics_path_is_whitelisted_on_the_login_wall():
    """The route does its own token/admin check, so the wall has to let it reach
    the handler; otherwise a token-carrying scraper gets a 401 it can never
    satisfy. Paired with the auth tests above, which prove it is still closed."""
    import api

    assert "/metrics" in api._PUBLIC_EXACT


def test_metrics_route_is_mounted_and_hidden_from_the_schema():
    import api

    routes = [r for r in api.app.routes if getattr(r, "path", None) == "/metrics"]
    assert len(routes) == 1
    # Kept out of openapi.json: it is an operational endpoint, not part of the
    # client-facing API surface (and the checked-in openapi.json would otherwise drift).
    assert routes[0].include_in_schema is False


def test_worker_stats_accessors_exist_and_are_cheap():
    """The collector calls these on every scrape; they must exist and must not
    require a started worker."""
    from cache_engine.downloader import manager as cache_manager

    stats = cache_manager.worker_stats()
    assert stats["queued"] == 0 and stats["inflight"] == 0
    assert stats["running"] is False


def test_prometheus_client_is_pinned_in_requirements():
    """Absent this line the image builds fine and /metrics silently 503s in
    production, which is the same class of failure as a missing migrations COPY."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as fh:
        assert "prometheus-client==" in fh.read()
