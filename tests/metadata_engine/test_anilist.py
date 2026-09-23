"""AniList is rate limited often. ``anilist_post`` retries 429 and 5xx, the
browse and trending reads serve the last good copy (tagged ``stale``) when the
live fetch fails, and ``media_card`` is the projection every discovery row uses.
"""

import asyncio

import metadata_engine.anilist as anilist


from metadata_engine.anilist import (
    MEDIA_SORTS,
    CATALOGUE_DEFAULT_SORT,
    _fetch_media_catalogue,
    media_card,
)




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


def _acoro(value):
    """Build an async function that ignores its args and returns ``value``."""
    async def _f(*args, **kwargs):
        return value
    return _f


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient returning a canned AniList payload."""

    def __init__(self, payload):
        self._payload = payload

    async def post(self, *args, **kwargs):
        return _FakeResp(self._payload)


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
    assert client.calls == anilist.MAX_RETRIES


def test_anilist_post_reraises_network_error_when_never_answered(monkeypatch):
    _no_sleep(monkeypatch)
    import httpx
    client = FakeClient([httpx.ConnectError("boom")] * anilist.MAX_RETRIES)
    try:
        asyncio.run(anilist.anilist_post(client, "query {}"))
        assert False, "expected the network error to propagate"
    except httpx.ConnectError:
        pass


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


def test_media_sort_tokens_map_to_anilist_enums():
    # Shared by the anime + manga AniList browses.
    assert set(MEDIA_SORTS) == {"trending", "popular", "score", "newest", "title"}
    assert MEDIA_SORTS[CATALOGUE_DEFAULT_SORT] == "TRENDING_DESC"
    assert MEDIA_SORTS["score"] == "SCORE_DESC"


async def test_anilist_browse_flags_upstream_error_as_unavailable(monkeypatch):
    # AniList returns HTTP 200 with an `errors` field on an outage (e.g. the whole
    # API being disabled). That must surface as `unavailable` (which the route turns
    # into the local-DB fallback), NOT an empty page, otherwise the hub would show
    # a misleading "nothing matched".
    async def _no_cache(*a, **k):
        return None
    monkeypatch.setattr(anilist, "get_cached_response", _no_cache)
    monkeypatch.setattr(anilist, "set_cached_response_shadowed", _no_cache)
    # The unavailable path now consults the stale shadow before giving up; stub it
    # so the test stays hermetic (no DB) and asserts the no-shadow → unavailable case.
    monkeypatch.setattr(anilist, "get_stale_response", _no_cache)

    client = _FakeClient({"errors": [{"message": "API disabled"}], "data": None})
    result = await _fetch_media_catalogue(client, "ANIME", "anime", None, "trending", 1, 30)
    assert result["unavailable"] is True
    assert result["items"] == []


async def test_anilist_browse_projects_and_tags_kind(monkeypatch):
    async def _no_cache(*a, **k):
        return None
    monkeypatch.setattr(anilist, "get_cached_response", _no_cache)
    monkeypatch.setattr(anilist, "set_cached_response_shadowed", _no_cache)
    monkeypatch.setattr(anilist, "get_stale_response", _no_cache)

    payload = {"data": {"Page": {
        "pageInfo": {"hasNextPage": True, "total": 100, "currentPage": 1},
        "media": [{"id": 21, "title": {"english": "One Piece"},
                   "coverImage": {"extraLarge": "u"}, "startDate": {"year": 1999},
                   "averageScore": 88}],
    }}}
    result = await _fetch_media_catalogue(_FakeClient(payload), "ANIME", "anime", None, "trending", 1, 30)
    assert result.get("unavailable") is None
    assert result["has_next"] is True and result["total"] == 100
    assert result["items"][0]["kind"] == "anime"  # re-tagged from the generic projection
    assert result["items"][0]["anilist_id"] == 21


def test_media_card_projection():
    media = {
        "id": 30002,
        "title": {"romaji": "Berserk", "english": "Berserk", "native": "ベルセルク"},
        "coverImage": {"extraLarge": "big.jpg", "large": "small.jpg"},
        "startDate": {"year": 1989},
        "averageScore": 93,
    }
    item = media_card(media)
    assert item["anilist_id"] == 30002
    assert item["title"] == "Berserk"
    assert item["poster"] == "big.jpg"       # prefers extraLarge
    assert item["year"] == 1989
    assert item["vote_average"] == 9.3        # 0-100 score -> 0-10 rating
    assert item["kind"] == "manga"


def test_media_card_handles_missing_score():
    item = media_card({"id": 1, "title": {"romaji": "X"}, "coverImage": {}, "startDate": {}})
    assert item["vote_average"] is None
    assert item["poster"] is None
    assert item["title"] == "X"
