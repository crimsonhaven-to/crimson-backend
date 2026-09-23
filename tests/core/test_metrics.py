"""What turns a metrics endpoint into an outage if it regresses: unbounded label
values, and a recording helper that raises into the /watch fan-out."""

import pytest

from core import metrics


class _FakeRoute:
    def __init__(self, path_format):
        self.path_format = path_format


def test_route_label_uses_the_template_not_the_path():
    scope = {"path": "/watch/1429/1/7", "route": _FakeRoute("/watch/{tmdb_id}/{season_number}/{episode_number}")}
    assert metrics.route_label(scope) == "/watch/{tmdb_id}/{season_number}/{episode_number}"


def test_route_label_collapses_unmatched_paths():
    """A scanner probing random URLs must not mint a series per probe."""
    assert {metrics.route_label({"path": f"/wp-admin/{i}"}) for i in range(50)} == {metrics.UNMATCHED_ROUTE}


def test_route_label_is_length_capped():
    assert len(metrics.route_label({"route": _FakeRoute("/" + "x" * 900)})) <= 200


@pytest.mark.parametrize(
    "raw, expected",
    [("get", "GET"), ("POST", "POST"), ("PROPFIND", "OTHER"), (None, "OTHER"), ("", "OTHER")],
)
def test_method_label_is_a_closed_vocabulary(raw, expected):
    assert metrics.method_label(raw) == expected


def test_record_helpers_never_raise_on_junk_input():
    metrics.record_http_request("GET", "/x", 200, 0.1)
    metrics.record_http_request(None, None, None, None)
    metrics.record_scraper_run(None, None, None)
    metrics.record_resolve(None, None, None)
    metrics.record_watch(None, None, None, None, None)
    metrics.record_cache_lookup(None, None)
    metrics.track_in_progress(None, 1)


def test_render_produces_prometheus_text():
    metrics.record_http_request("GET", "/health", 200, 0.01)
    payload, content_type = metrics.render()
    assert b"crimson_http_requests_total" in payload
    assert "text/plain" in content_type
