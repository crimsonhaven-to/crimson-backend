"""Reading Spotify's playlist entries across both API migrations, and token expiry."""

from datetime import datetime, timedelta, timezone

import pytest

from music_engine.spotify import (
    SpotifyClient,
    _token_still_valid,
    parse_playlist_entry,
    parse_track,
)

TRACK = {
    "id": "2FZcjBYK4dTt48q94pJbJD",
    "name": "Bass Persuades",
    "type": "track",
    "duration_ms": 202460,
    "track_number": 3,
    "disc_number": 1,
    "external_ids": {"isrc": "USSM12600001"},
    "artists": [{"id": "5YGY8feqx7naU7z4HrwZM6", "name": "Miley Cyrus"}, {"name": "Guest"}],
    "album": {
        "name": "Something Beautiful",
        "release_date": "2026-09-03",
        "artists": [{"name": "Miley Cyrus"}],
        "images": [
            {"url": "small", "width": 64},
            {"url": "large", "width": 640},
        ],
    },
}


def test_a_track_carries_everything_the_tags_need():
    track = parse_track(TRACK, "2026-09-10T00:00:00Z")
    assert track is not None
    assert (track.spotify_id, track.isrc, track.album) == (
        "2FZcjBYK4dTt48q94pJbJD", "USSM12600001", "Something Beautiful"
    )
    assert track.cover_url == "large"
    assert track.added_at == "2026-09-10T00:00:00Z"


def test_new_and_old_entry_shapes_both_read():
    assert parse_playlist_entry({"item": TRACK, "added_at": None}).title == "Bass Persuades"
    assert parse_playlist_entry({"track": TRACK, "added_at": None}).title == "Bass Persuades"


def test_local_files_episodes_and_blanks_are_skipped():
    assert parse_playlist_entry({"is_local": True, "item": TRACK}) is None
    assert parse_track({**TRACK, "type": "episode"}) is None
    assert parse_track({**TRACK, "is_local": True}) is None
    assert parse_track({**TRACK, "name": ""}) is None
    assert parse_track(None) is None


def test_token_validity_leaves_a_margin():
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    assert not _token_still_valid({"access_token": "t", "access_expires_at": soon})
    assert _token_still_valid({"access_token": "t", "access_expires_at": later})
    assert not _token_still_valid({"access_token": None, "access_expires_at": later})


def test_a_track_carries_its_artists_spotify_ids():
    track = parse_track(TRACK)
    assert track.artists == ["Miley Cyrus", "Guest"]
    assert track.spotify_artist_ids == ["5YGY8feqx7naU7z4HrwZM6"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("miley cyrus", "m1"), ("Miley", None), ("", None)],
)
async def test_an_artist_is_found_by_exact_name_only(monkeypatch, name, expected):
    asked = []

    async def fake_get(self, path, params=None):
        asked.append(params)
        return {"artists": {"items": [
            {"id": "m0", "name": "Miley Cyrus Tribute"},
            {"id": "m1", "name": "Miley Cyrus"},
        ]}}

    monkeypatch.setattr(SpotifyClient, "get", fake_get)
    found = await SpotifyClient(1).find_artist(name)
    assert (found or {}).get("id") == expected
    assert len(asked) == (1 if name else 0)
