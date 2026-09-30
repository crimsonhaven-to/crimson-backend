"""A year of listening. The risks are the same as for watching: a plausible
number that is wrong, and a day boundary that is the server's rather than the
viewer's."""

from datetime import datetime, timezone

from music_engine import wrapped


def _listen(track_id, when, seconds=180.0, artists=("Band",)):
    return {"id": track_id, "listened_at": when, "seconds": seconds,
            "title": f"Song {track_id}", "artists": list(artists)}


def test_the_local_year_starts_at_local_midnight():
    start, end = wrapped.year_bounds(2026, 120)
    assert start == datetime(2025, 12, 31, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc)


def test_nothing_heard_is_all_zeros():
    summary = wrapped.summarise([], 0)
    assert summary["plays"] == summary["minutes"] == summary["songs"] == 0
    assert summary["top_tracks"] == summary["top_artists"] == []


def test_songs_rank_by_plays_and_artists_by_time():
    day = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    rows = [
        _listen(1, day, 60.0, ("Short",)),
        _listen(1, day.replace(hour=13), 60.0, ("Short",)),
        _listen(2, day, 1200.0, ("Long",)),
    ]
    summary = wrapped.summarise(rows, 0)
    assert [t["row"]["id"] for t in summary["top_tracks"]] == [1, 2]
    assert summary["top_tracks"][0]["plays"] == 2
    assert [a["name"] for a in summary["top_artists"]] == ["Long", "Short"]
    assert summary["plays"] == 3
    assert summary["minutes"] == 22
    assert summary["songs"] == 2


def test_a_duet_counts_for_both_artists():
    rows = [_listen(1, datetime(2026, 3, 1, tzinfo=timezone.utc), 120.0, ("A", "B"))]
    summary = wrapped.summarise(rows, 0)
    assert {a["name"]: a["minutes"] for a in summary["top_artists"]} == {"A": 2, "B": 2}
    assert summary["artists"] == 2


def test_active_days_are_the_viewers_days():
    late = datetime(2026, 3, 1, 23, 30, tzinfo=timezone.utc)
    early = datetime(2026, 3, 2, 0, 30, tzinfo=timezone.utc)
    rows = [_listen(1, late), _listen(2, early)]
    assert wrapped.summarise(rows, 0)["active_days"] == 2
    assert wrapped.summarise(rows, 120)["active_days"] == 1


def test_build_asks_for_the_local_year(monkeypatch):
    asked = []
    monkeypatch.setattr(wrapped.listens_db, "between",
                        lambda user_id, lo, hi: asked.append((user_id, lo, hi)) or [])
    wrapped.build(7, 2026, 60)
    assert asked == [(7, *wrapped.year_bounds(2026, 60))]
