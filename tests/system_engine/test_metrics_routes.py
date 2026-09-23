"""/metrics is closed by default: forgetting METRICS_TOKEN must make it more
closed, never open."""

import os

from account_engine.deps import parse_bearer
from system_engine import metrics_routes


class _FakeRequest:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


async def _auth(headers, monkeypatch, token="", user=None):
    monkeypatch.setattr(metrics_routes.account_store, "get_user_by_session", lambda _: user)
    bearer = parse_bearer(headers.get("Authorization"))
    return await metrics_routes._authorized(_FakeRequest(headers), bearer, token)


async def test_denied_by_default(monkeypatch):
    assert await _auth({}, monkeypatch) is False
    assert await _auth({"X-Metrics-Token": "guess"}, monkeypatch) is False
    assert await _auth({"Authorization": "Bearer anything"}, monkeypatch) is False


async def test_the_configured_token_is_accepted(monkeypatch):
    assert await _auth({"X-Metrics-Token": "s3cret"}, monkeypatch, token="s3cret") is True
    assert await _auth({"Authorization": "Bearer s3cret"}, monkeypatch, token="s3cret") is True
    assert await _auth({"X-Metrics-Token": "wrong"}, monkeypatch, token="s3cret") is False


async def test_an_admin_session_is_accepted_but_not_a_plain_user(monkeypatch):
    headers = {"Authorization": "Bearer sess"}
    assert await _auth(headers, monkeypatch, user={"user_id": 1, "is_admin": True}) is True
    assert await _auth(headers, monkeypatch, user={"user_id": 2, "is_admin": False}) is False
    assert await _auth(headers, monkeypatch, user=None) is False


async def test_a_token_scrape_costs_no_database_lookup(monkeypatch):
    """A scrape every 15 to 60 seconds per replica must not become a query."""
    calls = []
    monkeypatch.setattr(metrics_routes.account_store, "get_user_by_session", calls.append)
    assert await metrics_routes._authorized(_FakeRequest({}), "s3cret", "s3cret") is True
    assert calls == []


def test_the_route_is_mounted_and_hidden_from_the_schema():
    import api

    routes = [r for r in api.app.routes if getattr(r, "path", None) == "/metrics"]
    assert len(routes) == 1
    assert routes[0].include_in_schema is False


def test_prometheus_client_is_pinned():
    """Without the pin the image builds fine and /metrics fails in production."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as fh:
        assert "prometheus-client==" in fh.read()
