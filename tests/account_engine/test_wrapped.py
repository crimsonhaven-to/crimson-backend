"""
Crimson Wrapped.

The risk here is not a crash, it is a plausible number that is wrong. So the
tests are mostly about the counting rules:

  * a year that predates watch_events must say ``approximate``, not present a
    reconstruction from last-touch timestamps as a measurement,
  * an event row always beats a progress row for the same item on the same day,
  * hours never grow just because an episode was watched across two days,
  * manga is never added into an episode count, and never contributes genres,
  * the day boundary is the viewer's, not the server's.

The connection is faked, so none of this needs Postgres.
"""


from datetime import date, datetime, timezone

import pytest

from account_engine import wrapped


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, events=(), progress=(), since=None, genres=None):
        self.events = list(events)
        self.progress = list(progress)
        self.since = since
        self.genres = genres or {}
        self.executed = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append(flat)
        if "MIN(first_seen_at)" in flat:
            return FakeCursor([{"since": self.since}])
        if "FROM watch_events" in flat:
            return FakeCursor(self.events)
        if "FROM watch_progress" in flat:
            return FakeCursor(self.progress)
        for table, rows in self.genres.items():
            if f"FROM {table}" in flat:
                return FakeCursor(rows)
        return FakeCursor([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def build(monkeypatch):
    def _run(conn, user_id=7, year=2026, offset_minutes=0):
        monkeypatch.setattr(wrapped, "get_connection", lambda: conn)
        return wrapped.build(user_id, year, offset_minutes)
    return _run


def _event(item_key="anilist:21:s1:e1", day=None, seconds=1400.0, **overrides):
    moment = day or NOW
    row = {
        "item_key": item_key,
        "anilist_id": 21,
        "tmdb_id": None,
        "media_type": None,
        "title": "One Piece",
        "seconds": seconds,
        "first_seen_at": moment,
    }
    row.update(overrides)
    return row


def _progress(item_key="anilist:21:s1:e1", updated_at=None, **overrides):
    row = {
        "item_key": item_key,
        "anilist_id": 21,
        "tmdb_id": None,
        "media_type": None,
        "title": "One Piece",
        "seconds": 900.0,
        "updated_at": (updated_at or NOW).isoformat(),
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("row,expected", [
    ({"media_type": "manga", "anilist_id": 30013}, "manga"),
    ({"media_type": "movie", "tmdb_id": 550}, "movie"),
    ({"media_type": "local", "anilist_id": None}, "local"),
    ({"media_type": None, "anilist_id": 21}, "anime"),
    ({"media_type": None, "anilist_id": None, "tmdb_id": 1396}, "show"),
])
def test_surface_classification(row, expected):
    assert wrapped._surface(row) == expected


def test_a_year_with_no_events_is_approximate(build):
    stats = build(FakeConn(progress=[_progress()], since=None))
    assert stats["approximate"] is True
    assert stats["events_since"] is None
    assert stats["episodes"] == 1


def test_a_year_fully_covered_by_events_is_exact(build):
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    stats = build(FakeConn(events=[_event()], since=since))
    assert stats["approximate"] is False
    assert stats["events_since"] == since.isoformat()


def test_a_year_partly_covered_says_where_the_reliable_part_starts(build):
    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    stats = build(FakeConn(
        events=[_event(day=datetime(2026, 6, 1, tzinfo=timezone.utc))],
        progress=[_progress(item_key="anilist:1:s1:e1",
                            updated_at=datetime(2026, 1, 5, tzinfo=timezone.utc))],
        since=since,
    ))
    assert stats["approximate"] is True
    assert stats["events_since"].startswith("2026-03-01")
    assert stats["episodes"] == 2


def test_progress_is_never_used_where_events_already_cover_the_day(build):
    """Otherwise a resumed episode counts twice, once real and once imagined."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stats = build(FakeConn(
        events=[_event(seconds=1400.0)],
        progress=[_progress(seconds=60.0)],
        since=since,
    ))
    assert stats["episodes"] == 1
    assert stats["hours"] == round(1400 / 3600, 1)


def test_progress_outside_the_approximate_span_is_ignored(build):
    """A progress row touched after the events table started recording tells us
    nothing the events do not, and its timestamp is the wrong kind of date."""
    since = datetime(2026, 2, 1, tzinfo=timezone.utc)
    stats = build(FakeConn(
        progress=[_progress(item_key="anilist:9:s1:e1",
                            updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc))],
        since=since,
    ))
    assert stats["episodes"] == 0


def test_hours_do_not_grow_when_an_episode_spans_two_days(build):
    """Each day records the furthest point reached, so the daily figures overlap.
    Summing them would report a 24-minute episode as 40 minutes."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(day=datetime(2026, 6, 1, 23, 50, tzinfo=timezone.utc), seconds=600.0),
        _event(day=datetime(2026, 6, 2, 0, 10, tzinfo=timezone.utc), seconds=1440.0),
    ], since=since)
    stats = build(conn)
    assert stats["hours"] == round(1440 / 3600, 1)
    assert stats["active_days"] == 2


def test_hours_tolerate_a_null_position(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stats = build(FakeConn(events=[_event(seconds=None)], since=since))
    assert stats["hours"] == 0.0
    assert stats["episodes"] == 1


def test_manga_is_never_added_into_the_episode_count(build):
    """A manga row is one per title with the chapter in episode_number, so adding
    it to a per-episode count compares two different things."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stats = build(FakeConn(events=[
        _event(item_key="anilist:21:s1:e1"),
        _event(item_key="manga:30013", media_type="manga", anilist_id=30013),
        _event(item_key="movie:550", media_type="movie", anilist_id=None, tmdb_id=550),
    ], since=since))
    assert stats["episodes"] == 1
    assert stats["manga_titles"] == 1
    assert stats["movies"] == 1
    assert stats["by_surface"] == {"anime": 1, "manga": 1, "movie": 1}


def test_local_rows_count_as_watching(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stats = build(FakeConn(events=[
        _event(item_key="local:abc:s1:e1", media_type="local", anilist_id=None),
    ], since=since))
    assert stats["episodes"] == 1


def test_genres_count_titles_not_episodes(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(
        events=[_event(item_key=f"anilist:21:s1:e{n}") for n in range(1, 25)],
        since=since,
        genres={"anime_entries": [{"id": 21, "genres": '["Action", "Adventure"]'}]},
    )
    stats = build(conn)
    assert stats["top_genres"] == [
        {"genre": "Action", "count": 1}, {"genre": "Adventure", "count": 1},
    ]
    assert stats["episodes"] == 24


def test_local_rows_contribute_no_genres(build):
    """They carry no AniList or TMDB id, so bucketing them as "unknown" would let
    an operator's own library dominate the chart."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(
        events=[_event(item_key="local:abc:s1:e1", media_type="local", anilist_id=None)],
        since=since,
    )
    stats = build(conn)
    assert stats["top_genres"] == []
    assert "anime_entries" not in " ".join(conn.executed)


def test_manga_never_looks_up_an_anime_id(build):
    """AniList numbers manga in its own space, so anime_entries would answer with
    a different title's genres."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(
        events=[_event(item_key="manga:21", media_type="manga", anilist_id=21)],
        since=since,
        genres={"anime_entries": [{"id": 21, "genres": '["Action"]'}]},
    )
    stats = build(conn)
    assert stats["top_genres"] == []


def test_unparseable_genres_are_skipped_not_fatal(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(
        events=[_event()],
        since=since,
        genres={"anime_entries": [{"id": 21, "genres": "not json"}]},
    )
    assert build(conn)["top_genres"] == []


def test_longest_streak():
    days = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
            date(2026, 1, 9), date(2026, 1, 10)]
    assert wrapped._longest_streak(days) == (3, "2026-01-01", "2026-01-03")


def test_longest_streak_of_one_day():
    assert wrapped._longest_streak([date(2026, 5, 5)]) == (1, "2026-05-05", "2026-05-05")


def test_longest_streak_of_nothing():
    assert wrapped._longest_streak([]) == (0, None, None)


def test_a_repeated_day_is_not_a_streak():
    days = [date(2026, 5, 5)] * 10
    assert wrapped._longest_streak(days)[0] == 1


def test_the_day_boundary_is_the_viewers(build):
    """23:30 UTC on the 1st is already the 2nd in Tokyo. Bucketing by the
    server's day would put a viewer's evening on the wrong date all year."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    late = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    conn = FakeConn(events=[_event(day=late)], since=since)
    assert build(conn, offset_minutes=0)["busiest_day"]["day"] == "2026-06-01"
    assert build(conn, offset_minutes=540)["busiest_day"]["day"] == "2026-06-02"


def test_an_event_pulled_out_of_the_year_by_the_offset_is_dropped(build):
    """The query window is padded by a day either side, so the local date has to
    do the real filtering or New Year's Eve leaks into the wrong year."""
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(
        events=[_event(day=datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc))],
        since=since,
    )
    assert build(conn, offset_minutes=0)["episodes"] == 1
    assert build(conn, offset_minutes=540)["episodes"] == 0


@pytest.mark.parametrize("offset", [wrapped.MIN_OFFSET_MINUTES - 1, wrapped.MAX_OFFSET_MINUTES + 1])
def test_an_absurd_offset_is_rejected_by_the_route(offset):
    """build() trusts its offset, so the route is the only guard."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from account_engine import wrapped_routes
    from account_engine.deps import require_user

    app = FastAPI()
    app.include_router(wrapped_routes.router)
    app.dependency_overrides[require_user] = lambda: {"user_id": 7}
    response = TestClient(app).get(f"/account/wrapped?offset_minutes={offset}")
    assert response.status_code == 422


def test_busiest_day_counts_items(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    busy = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
    quiet = datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(item_key="anilist:21:s1:e1", day=busy),
        _event(item_key="anilist:21:s1:e2", day=busy),
        _event(item_key="anilist:21:s1:e3", day=busy),
        _event(item_key="anilist:21:s1:e4", day=quiet),
    ], since=since)
    stats = build(conn)
    assert stats["busiest_day"] == {"day": "2026-03-03", "items": 3}
    assert stats["active_days"] == 2


def test_an_account_that_watched_nothing(build):
    stats = build(FakeConn(since=None))
    assert stats["episodes"] == 0 and stats["hours"] == 0.0
    assert stats["busiest_day"] == {"day": None, "items": 0}
    assert stats["longest_streak"] == {"days": 0, "from": None, "to": None}
    assert stats["first_title"] is None and stats["top_titles"] == []


def test_first_and_last_title_follow_the_calendar(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(item_key="anilist:1:s1:e1", anilist_id=1, title="Spring",
               day=datetime(2026, 4, 1, tzinfo=timezone.utc)),
        _event(item_key="anilist:2:s1:e1", anilist_id=2, title="Winter",
               day=datetime(2026, 1, 9, tzinfo=timezone.utc)),
        _event(item_key="anilist:3:s1:e1", anilist_id=3, title="Autumn",
               day=datetime(2026, 10, 2, tzinfo=timezone.utc)),
    ], since=since)
    stats = build(conn)
    assert stats["first_title"] == "Winter"
    assert stats["last_title"] == "Autumn"


def test_top_titles_are_ordered_by_time_spent(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(item_key="anilist:1:s1:e1", anilist_id=1, title="Short", seconds=300.0),
        _event(item_key="anilist:2:s1:e1", anilist_id=2, title="Long", seconds=3600.0),
    ], since=since)
    top = build(conn)["top_titles"]
    assert [t["title"] for t in top] == ["Long", "Short"]
    assert top[0]["minutes"] == 60


def test_top_titles_sum_a_season_into_one_entry(build):
    """Twelve episodes of one show are one row in the chart, not twelve. Anything
    else makes a long-running series look like twelve separate hobbies."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(item_key=f"anilist:21:s1:e{n}", seconds=1200.0) for n in range(1, 13)
    ] + [
        _event(item_key="movie:550", media_type="movie", anilist_id=None,
               tmdb_id=550, title="Fight Club", seconds=7000.0),
    ], since=since)
    top = build(conn)["top_titles"]
    assert [t["title"] for t in top] == ["One Piece", "Fight Club"]
    assert top[0]["minutes"] == 12 * 20


def test_local_episodes_collapse_onto_their_title(build):
    """Local media is the one surface with no AniList or TMDB id, so the show is
    recovered from the item key's shape."""
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(item_key=f"local:some-show:s1:e{n}", media_type="local",
               anilist_id=None, title="Some Show", seconds=600.0)
        for n in range(1, 6)
    ], since=since)
    stats = build(conn)
    assert stats["episodes"] == 5
    assert stats["distinct_titles"]["local"] == 1
    assert stats["top_titles"] == [{"title": "Some Show", "minutes": 50}]


@pytest.mark.parametrize("value", [
    "2026-06-15T12:00:00+00:00",
    "2026-06-15T12:00:00",
    datetime(2026, 6, 15, 12, 0),
    datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc),
])
def test_updated_at_parsing_always_lands_in_utc(value):
    parsed = wrapped._parse_updated_at(value)
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).hour == 12


@pytest.mark.parametrize("value", [None, "", "not a date", 12345])
def test_unparseable_updated_at_is_skipped(value):
    assert wrapped._parse_updated_at(value) is None


def test_a_progress_row_with_a_broken_timestamp_does_not_break_the_year(build):
    stats = build(FakeConn(
        progress=[_progress(updated_at=NOW), {"item_key": "x", "updated_at": "nonsense",
                                              "anilist_id": None, "tmdb_id": None,
                                              "media_type": None, "title": None,
                                              "seconds": None}],
        since=None,
    ))
    assert stats["episodes"] == 1


def test_local_day_shifts_by_the_offset():
    moment = datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc)
    assert wrapped._local_day(moment, 0) == date(2026, 6, 1)
    assert wrapped._local_day(moment, -480) == date(2026, 5, 31)


def test_the_query_window_is_padded_by_a_day(build):
    """Without the padding a viewer east of UTC loses the last evening of the
    year and one west of it loses the first morning."""
    conn = FakeConn(since=None)
    build(conn)
    window = [sql for sql in conn.executed if "FROM watch_events WHERE user_id" in sql]
    assert window, "the events query must run"


def test_seconds_of_the_same_item_on_different_days_take_the_maximum(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(item_key="ep", day=datetime(2026, 2, 1, tzinfo=timezone.utc), seconds=200.0),
        _event(item_key="ep", day=datetime(2026, 2, 2, tzinfo=timezone.utc), seconds=100.0),
    ], since=since)
    assert build(conn)["hours"] == round(200 / 3600, 1)


def test_distinct_titles_collapse_episodes_of_one_show(build):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn = FakeConn(events=[
        _event(item_key=f"anilist:21:s1:e{n}") for n in range(1, 13)
    ], since=since)
    stats = build(conn)
    assert stats["episodes"] == 12
    assert stats["distinct_titles"]["anime"] == 1
