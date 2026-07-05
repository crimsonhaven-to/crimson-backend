"""AniList resilience: request retry/backoff (tier 1) + serve-stale-on-error (tier 2).

AniList is the single upstream behind every discovery/metadata endpoint and is
frequently rate-limited (429) or transiently 5xx. These tests pin the two guards
added for that, without touching the network or the DB:

  * ``anilist_post`` retries 429 / 5xx and returns the final response;
  * ``fetch_trending_manga`` / ``fetch_manga_catalogue`` serve the last known good
    copy (tagged ``stale``) when the live fetch fails, instead of an empty row / 503.
"""

import asyncio

import metadata_engine.anilist as anilist


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeClient:
    """Yields a queued sequence of responses (or raises a queued exception)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def post(self, url, json=None, timeout=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _no_sleep(monkeypatch):
    async def _fake_sleep(_):
        return None
    monkeypatch.setattr(anilist.asyncio, "sleep", _fake_sleep)


# --- tier 1: anilist_post retry/backoff ------------------------------------
def test_anilist_post_retries_429_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    client = FakeClient([
        FakeResponse(429, headers={"Retry-After": "1"}),
        FakeResponse(200, {"data": {"ok": True}}),
    ])
    resp = asyncio.run(anilist.anilist_post(client, "query {}"))
    assert resp.status_code == 200
    assert client.calls == 2  # retried once


def test_anilist_post_returns_last_response_after_exhausting_5xx(monkeypatch):
    _no_sleep(monkeypatch)
    client = FakeClient([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    resp = asyncio.run(anilist.anilist_post(client, "query {}"))
    # Exhausted → caller still sees a real response (503) and degrades itself.
    assert resp.status_code == 503
    assert client.calls == anilist.Config.MAX_RETRIES


def test_anilist_post_reraises_network_error_when_never_answered(monkeypatch):
    _no_sleep(monkeypatch)
    import httpx
    client = FakeClient([httpx.ConnectError("boom")] * anilist.Config.MAX_RETRIES)
    try:
        asyncio.run(anilist.anilist_post(client, "query {}"))
        assert False, "expected the network error to propagate"
    except httpx.ConnectError:
        pass


# --- tier 2: serve-stale-on-error ------------------------------------------
def test_trending_manga_serves_stale_on_outage(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(anilist, "get_cached_response", _acoro(None))
    monkeypatch.setattr(anilist, "get_stale_response", _acoro([{"anilist_id": 1, "kind": "manga"}]))

    async def _run():
        client = FakeClient([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
        return await anilist.fetch_trending_manga(client, limit=12)

    out = asyncio.run(_run())
    assert out["stale"] is True
    assert out["items"] == [{"anilist_id": 1, "kind": "manga"}]


def test_trending_manga_fresh_success_is_not_stale(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(anilist, "get_cached_response", _acoro(None))
    saved = {}

    async def _save(key, data, ttl_seconds=None):
        saved["key"], saved["data"] = key, data
    monkeypatch.setattr(anilist, "set_cached_response_shadowed", _save)

    media = {"data": {"Page": {"media": [
        {"id": 7, "title": {"romaji": "X"}, "coverImage": {"large": "c.jpg"}, "startDate": {"year": 2020}},
    ]}}}

    async def _run():
        client = FakeClient([FakeResponse(200, media)])
        return await anilist.fetch_trending_manga(client, limit=12)

    out = asyncio.run(_run())
    assert out["stale"] is False
    assert out["items"][0]["anilist_id"] == 7
    assert saved["data"], "a successful fetch must refresh the stale shadow"


def test_media_catalogue_serves_stale_page_on_outage(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(anilist, "get_cached_response", _acoro(None))
    shadow = {"items": [{"anilist_id": 9, "kind": "manga"}], "page": 1, "has_next": True, "total": 42}
    monkeypatch.setattr(anilist, "get_stale_response", _acoro(shadow))

    async def _run():
        client = FakeClient([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
        return await anilist.fetch_manga_catalogue(client, page=1)

    out = asyncio.run(_run())
    assert out.get("stale") is True
    assert out.get("unavailable") is None
    assert out["items"] == shadow["items"]
    assert out["total"] == 42


def test_media_catalogue_unavailable_when_no_shadow(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(anilist, "get_cached_response", _acoro(None))
    monkeypatch.setattr(anilist, "get_stale_response", _acoro(None))

    async def _run():
        client = FakeClient([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
        return await anilist.fetch_manga_catalogue(client, page=1)

    out = asyncio.run(_run())
    assert out.get("unavailable") is True
    assert out["items"] == []


def _acoro(value):
    """Build an async function that ignores its args and returns ``value``."""
    async def _f(*args, **kwargs):
        return value
    return _f
