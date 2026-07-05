"""Pure-logic tests for the shows/movies/manga browse catalogues.

These cover the parts of the new /catalogue/shows|movies|manga endpoints that
don't need a DB or the network: the defensive genre decode, the popular-first
sort key, the manga sort-token map, and the AniList manga projection. The DB
readers themselves (get_shows_catalogue_items / get_movies_catalogue_items) are
thin SELECT + project loops exercised end-to-end by the running service.
"""

import metadata_engine.anilist as anilist
from web.queries import _decode_genres
from metadata_engine.anilist import (
    _MEDIA_SORTS,
    CATALOGUE_DEFAULT_SORT,
    MANGA_DEFAULT_SORT,
    _fetch_media_catalogue,
    _manga_item,
)


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


def test_decode_genres_is_defensive():
    assert _decode_genres('["Drama", "Crime"]') == ["Drama", "Crime"]
    assert _decode_genres(None) == []          # rows synced before genres existed
    assert _decode_genres("") == []
    assert _decode_genres("not json") == []    # malformed → empty, never raises


def _sort_key(x):
    # Mirror of the ORDER used by get_shows/movies_catalogue_items: popularity
    # desc (NULLS LAST via the -inf sentinel), then newest year, then title.
    return (
        -(x["popularity"] if isinstance(x["popularity"], (int, float)) else float("-inf")),
        -(int(x["year"]) if (x["year"] or "").isdigit() else 0),
        (x["title"] or "").lower(),
    )


def test_popularity_sort_puts_popular_first_and_nulls_last():
    rows = [
        {"popularity": None, "year": "2001", "title": "b"},
        {"popularity": 5.0, "year": "1999", "title": "a"},
        {"popularity": 50.0, "year": "2000", "title": "c"},
    ]
    assert [r["title"] for r in sorted(rows, key=_sort_key)] == ["c", "a", "b"]


def test_media_sort_tokens_map_to_anilist_enums():
    # Shared by the anime + manga AniList browses.
    assert set(_MEDIA_SORTS) == {"trending", "popular", "score", "newest", "title"}
    assert _MEDIA_SORTS[CATALOGUE_DEFAULT_SORT] == "TRENDING_DESC"
    assert _MEDIA_SORTS["score"] == "SCORE_DESC"
    # Back-compat alias still points at the default.
    assert MANGA_DEFAULT_SORT == CATALOGUE_DEFAULT_SORT


async def test_anilist_browse_flags_upstream_error_as_unavailable(monkeypatch):
    # AniList returns HTTP 200 with an `errors` field on an outage (e.g. the whole
    # API being disabled). That must surface as unavailable (→ 503), NOT an empty
    # page — otherwise the hub shows a misleading "nothing matched".
    async def _no_cache(*a, **k):
        return None
    monkeypatch.setattr(anilist, "get_cached_response", _no_cache)
    monkeypatch.setattr(anilist, "set_cached_response", _no_cache)

    client = _FakeClient({"errors": [{"message": "API disabled"}], "data": None})
    result = await _fetch_media_catalogue(client, "ANIME", "anime", None, "trending", 1, 30)
    assert result["unavailable"] is True
    assert result["items"] == []


async def test_anilist_browse_projects_and_tags_kind(monkeypatch):
    async def _no_cache(*a, **k):
        return None
    monkeypatch.setattr(anilist, "get_cached_response", _no_cache)
    monkeypatch.setattr(anilist, "set_cached_response", _no_cache)

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


def test_manga_item_projection():
    node = {
        "id": 30013,
        "title": {"english": "One Piece", "romaji": "One Piece"},
        "coverImage": {"extraLarge": "u", "large": "l"},
        "startDate": {"year": 1997},
        "averageScore": 90,
    }
    assert _manga_item(node) == {
        "anilist_id": 30013,
        "title": "One Piece",
        "poster": "u",
        "year": 1997,
        "vote_average": 9.0,  # AniList 0-100 → card 0-10
        "kind": "manga",
    }
