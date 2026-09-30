"""A listen report is kept only when it could be true: a real track, a moment
in the recent past, and no more seconds than the song could have played."""

from datetime import datetime, timedelta, timezone

import pytest

from music_engine import listens

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
LENGTHS = {1: 200_000, 2: 0}


def _keep(*reported):
    return listens.plausible(list(reported), LENGTHS, NOW)


def test_a_normal_listen_is_kept_as_reported():
    assert _keep((1, NOW, 120.0)) == [(1, NOW, 120.0)]


def test_an_unknown_track_is_dropped():
    assert _keep((9, NOW, 120.0)) == []


def test_a_skipped_song_does_not_count():
    assert _keep((1, NOW, 29.0)) == []


def test_seconds_are_capped_at_the_song_length_with_slack():
    [(_, _, seconds)] = _keep((1, NOW, 5000.0))
    assert seconds == pytest.approx(220.0)


def test_a_song_of_unknown_length_is_capped_at_an_hour():
    [(_, _, seconds)] = _keep((2, NOW, 90_000.0))
    assert seconds == listens.UNKNOWN_LENGTH_CAP


def test_offline_listens_from_last_week_are_kept_but_not_from_last_quarter():
    week = NOW - timedelta(days=7)
    quarter = NOW - timedelta(days=90)
    assert _keep((1, week, 60.0), (1, quarter, 60.0)) == [(1, week, 60.0)]


def test_a_clock_far_in_the_future_is_dropped():
    assert _keep((1, NOW + timedelta(days=1), 60.0)) == []


def test_a_naive_moment_is_read_as_utc():
    naive = NOW.replace(tzinfo=None)
    assert _keep((1, naive, 60.0)) == [(1, NOW, 60.0)]


def test_record_looks_lengths_up_once_and_inserts_what_is_plausible(monkeypatch):
    looked_up, inserted = [], []
    monkeypatch.setattr(listens, "utc_now", lambda: NOW)
    monkeypatch.setattr(listens.listens_db, "durations",
                        lambda ids: looked_up.append(ids) or LENGTHS)
    monkeypatch.setattr(listens.listens_db, "insert",
                        lambda user_id, rows: inserted.append((user_id, rows)) or len(rows))
    assert listens.record(7, [(1, NOW, 60.0), (1, NOW, 10.0), (9, NOW, 60.0)]) == 1
    assert looked_up == [[1, 9]]
    assert inserted == [(7, [(1, NOW, 60.0)])]


def test_an_empty_report_touches_nothing(monkeypatch):
    monkeypatch.setattr(listens.listens_db, "durations", lambda ids: 1 / 0)
    assert listens.record(7, []) == 0
