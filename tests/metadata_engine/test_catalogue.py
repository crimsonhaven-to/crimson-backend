"""``search_anime_entries`` picks a tmdb id from one of three places and a poster
from one of two tables, and must emit the same shape as the TMDB search it partly
replaces. The database is faked.
"""


from metadata_engine.catalogue import decode_genres as _decode_genres


import pytest

import metadata_engine.catalogue as queries





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

    def execute(self, sql, params=None):
        self.cursor_obj.execute(sql, params)
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _with_rows(monkeypatch, rows):
    conn = _FakeConn(rows)
    monkeypatch.setattr(queries, "get_connection", lambda: conn)
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


def test_decode_genres_is_defensive():
    assert _decode_genres('["Drama", "Crime"]') == ["Drama", "Crime"]
    assert _decode_genres(None) == []          # rows synced before genres existed
    assert _decode_genres("") == []
    assert _decode_genres("not json") == []    # malformed → empty, never raises


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
    monkeypatch.setattr(queries, "get_connection", lambda: called.append(1))
    assert queries.search_anime_entries("   ") == []
    assert queries.search_anime_entries("") == []
    assert called == []


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
    monkeypatch.setattr(queries, "get_connection", _boom)
    assert queries.search_anime_entries("one piece") == []
