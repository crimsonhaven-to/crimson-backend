"""
Local-first /search/anime: the row projection, the LIKE escaping and the
fall-through to TMDB.

``search_anime_entries`` is more than the thin SELECT-and-project loop the other
catalogue readers are (see test_catalogue_browse.py's note on why those are left
to the running service): it picks a tmdb id from one of three places, picks a
poster from one of two tables depending on which, and has to emit a shape
byte-compatible with the TMDB search it partly replaces. That branching is worth
pinning, so the DB is faked here rather than skipped.

No database and no network, as everywhere else in this suite.
"""

import pytest

import web.queries as queries
import web.routes.discovery as discovery
from core.config import get_settings


# --- fakes ------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_rows(monkeypatch, rows):
    conn = _FakeConn(rows)
    monkeypatch.setattr(queries, "get_db_connection", lambda: conn)
    return conn


def _row(**overrides):
    """A matched anime_entries row, joined. Defaults to a plain mapped TV season."""
    row = {
        "anilist_id": 21,
        "title_romaji": "One Piece",
        "title_english": "One Piece",
        "title_native": "ONE PIECE",
        "start_year": 1999,
        "tmdb_movie_id": None,
        "season_tmdb_id": 37854,
        "season_number": 1,
        "extra_tmdb_id": None,
        "show_poster": "/poster.jpg",
        "movie_poster": None,
    }
    row.update(overrides)
    return row


# --- LIKE escaping ----------------------------------------------------------

@pytest.mark.parametrize("raw,escaped", [
    ("one piece", "one piece"),
    ("100%", r"100\%"),
    ("a_b", r"a\_b"),
    ("back\\slash", r"back\\slash"),
    (r"%_\\", r"\%\_\\\\"),
])
def test_like_wildcards_are_escaped(raw, escaped):
    """Unescaped, a query of "%" matches the whole catalogue and "_" matches every
    single-character title: a denial of service dressed as a typo."""
    assert queries._escape_like(raw) == escaped


def test_a_wildcard_query_is_bound_as_a_literal(monkeypatch):
    conn = _with_rows(monkeypatch, [])
    queries.search_anime_entries("50%")
    # The pattern travels as a bound parameter, never interpolated into the SQL.
    assert conn.cursor_obj.params["contains"] == r"%50\%%"
    assert "50" not in conn.cursor_obj.sql


def test_a_blank_query_never_reaches_the_database(monkeypatch):
    called = []
    monkeypatch.setattr(queries, "get_db_connection", lambda: called.append(1))
    assert queries.search_anime_entries("   ") == []
    assert queries.search_anime_entries("") == []
    assert called == []


# --- the projection ---------------------------------------------------------

def test_a_mapped_season_projects_the_tmdb_search_shape(monkeypatch):
    _with_rows(monkeypatch, [_row()])
    (item,) = queries.search_anime_entries("one piece")

    assert item == {
        "title": "One Piece",
        "tmdb_id": 37854,
        "anilist_id": 21,
        "poster": "https://image.tmdb.org/t/p/w500/poster.jpg",
        "year": "1999",
        "vote_average": None,
    }


def test_vote_average_is_present_and_null(monkeypatch):
    """anime_entries carries no score. The key must still exist: the frontend
    sorts hubs on it, and an absent key and a null one are not the same to JS."""
    _with_rows(monkeypatch, [_row()])
    (item,) = queries.search_anime_entries("one piece")
    assert "vote_average" in item
    assert item["vote_average"] is None


def test_english_title_wins_then_romaji_then_native(monkeypatch):
    _with_rows(monkeypatch, [
        _row(anilist_id=1, title_english="English", title_romaji="Romaji"),
        _row(anilist_id=2, title_english=None, title_romaji="Romaji"),
        _row(anilist_id=3, title_english=None, title_romaji=None, title_native="ネイティブ"),
    ])
    assert [i["title"] for i in queries.search_anime_entries("x")] == [
        "English", "Romaji", "ネイティブ",
    ]


def test_an_entry_with_no_title_at_all_is_dropped(monkeypatch):
    """AniList titles that never resolved are useless in a suggestion list."""
    _with_rows(monkeypatch, [
        _row(anilist_id=1, title_english=None, title_romaji=None, title_native=None),
        _row(anilist_id=2),
    ])
    assert [i["anilist_id"] for i in queries.search_anime_entries("x")] == [2]


def test_extras_supply_the_tmdb_id_when_there_is_no_season(monkeypatch):
    _with_rows(monkeypatch, [_row(season_tmdb_id=None, season_number=None, extra_tmdb_id=999)])
    (item,) = queries.search_anime_entries("x")
    assert item["tmdb_id"] == 999


def test_a_film_keyed_by_its_own_movie_id_takes_the_movie_poster(monkeypatch):
    """tmdb_shows and tmdb_movies are separate id spaces whose numbers overlap, so
    the poster must come from the table matching how the row is keyed."""
    _with_rows(monkeypatch, [_row(
        season_tmdb_id=None, season_number=None, extra_tmdb_id=None,
        tmdb_movie_id=1014505, show_poster="/wrong.jpg", movie_poster="/film.jpg",
    )])
    (item,) = queries.search_anime_entries("x")

    assert item["tmdb_id"] is None
    assert item["poster"] == "https://image.tmdb.org/t/p/w500/film.jpg"


def test_a_missing_poster_is_null_not_a_broken_url(monkeypatch):
    """tmdb_shows is sparse: it holds only shows somebody has opened once."""
    _with_rows(monkeypatch, [_row(show_poster=None)])
    (item,) = queries.search_anime_entries("x")
    assert item["poster"] is None


def test_a_missing_year_is_null_and_a_present_one_is_a_string(monkeypatch):
    """TMDB slices its year out of a date string, so the shapes must agree."""
    _with_rows(monkeypatch, [_row(anilist_id=1, start_year=None), _row(anilist_id=2, start_year=2013)])
    a, b = queries.search_anime_entries("x")
    assert a["year"] is None
    assert b["year"] == "2013"


def test_the_scan_is_capped(monkeypatch):
    """A one-letter query must not sort the whole catalogue."""
    conn = _with_rows(monkeypatch, [])
    queries.search_anime_entries("a", limit=10_000)
    assert conn.cursor_obj.params["cap"] == queries._SEARCH_SCAN_CAP


def test_a_database_error_degrades_to_no_results(monkeypatch):
    """Search is one of five parallel calls per keystroke; one failing surface
    must not take the search bar down."""
    def _boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(queries, "get_db_connection", _boom)
    assert queries.search_anime_entries("one piece") == []


# --- the endpoint: local first, TMDB as backstop ----------------------------

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

    monkeypatch.setattr(discovery, "search_anime_entries", _local)
    monkeypatch.setattr(discovery, "fetch_tmdb_search_results", _remote)
    monkeypatch.setattr(discovery, "http_client", lambda *a, **k: _FakeHTTPClient())
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
