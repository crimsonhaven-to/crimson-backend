"""/search/anime answers from the local catalogue first and falls through to TMDB
only when it has too few hits.
"""



import metadata_engine.discovery_routes as discovery

import metadata_engine.search as search

from core.config import get_settings


class _FakeHTTPClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _wire(monkeypatch, local, remote):
    calls = {"remote": 0}

    def _local(query, *args, **kwargs):
        return list(local)

    async def _remote(client, query, *args, **kwargs):
        calls["remote"] += 1
        return list(remote)

    monkeypatch.setattr(search, "search_anime_entries", _local)
    monkeypatch.setattr(search, "fetch_tmdb_search_results", _remote)
    monkeypatch.setattr(search, "http_client", lambda *a, **k: _FakeHTTPClient())
    return calls


def _suggestion(anilist_id, title):
    return {"title": title, "tmdb_id": None, "anilist_id": anilist_id,
            "poster": None, "year": None, "vote_average": None}


async def test_enough_local_hits_skip_tmdb_entirely(monkeypatch):
    local = [_suggestion(i, f"t{i}") for i in range(1, 6)]
    calls = _wire(monkeypatch, local, [_suggestion(99, "remote")])

    body = await discovery.search_anime_by_name(settings=get_settings(), query_name="one piece")

    assert calls["remote"] == 0, "the whole point is not making this call"
    assert body["count"] == 5
    assert [s["anilist_id"] for s in body["suggestions"]] == [1, 2, 3, 4, 5]


async def test_too_few_local_hits_fall_through_to_tmdb(monkeypatch):
    """A title added upstream since the last Fribb sync is not in anime_entries
    yet, so the remote search still has to run."""
    calls = _wire(monkeypatch, [], [_suggestion(99, "brand new")])

    body = await discovery.search_anime_by_name(settings=get_settings(), query_name="brand new")

    assert calls["remote"] == 1
    assert [s["anilist_id"] for s in body["suggestions"]] == [99]


async def test_the_merge_dedups_on_anilist_id_and_keeps_local_first(monkeypatch):
    """Local rows are ranked against the query; TMDB's order is TMDB's own
    popularity, so a duplicate keeps the local one."""
    local = [_suggestion(1, "local one")]
    remote = [_suggestion(1, "remote duplicate"), _suggestion(2, "remote only")]
    _wire(monkeypatch, local, remote)

    body = await discovery.search_anime_by_name(settings=get_settings(), query_name="x")

    assert [s["anilist_id"] for s in body["suggestions"]] == [1, 2]
    assert body["suggestions"][0]["title"] == "local one"


async def test_the_response_envelope_is_unchanged(monkeypatch):
    """The client reads .suggestions and nothing else; the rest of the envelope is
    what the endpoint returned before it went local-first."""
    _wire(monkeypatch, [_suggestion(1, "a"), _suggestion(2, "b"), _suggestion(3, "c")], [])

    body = await discovery.search_anime_by_name(settings=get_settings(), query_name="abc")

    assert set(body) == {"success", "query", "count", "suggestions"}
    assert body["success"] is True
    assert body["query"] == "abc"
    assert body["count"] == len(body["suggestions"])
