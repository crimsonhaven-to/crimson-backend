"""Playlist ids from whatever a user pastes, and how tracks are keyed."""

import pytest

from music_engine.library import parse_playlist_id, track_key
from music_engine.provider import ImportedTrack


@pytest.mark.parametrize(
    "value",
    [
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=1234",
        "https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        "37i9dQZF1DXcBWIGoYBM5M",
    ],
)
def test_playlist_ids_in_every_spelling(value):
    assert parse_playlist_id(value) == "37i9dQZF1DXcBWIGoYBM5M"


def test_liked_songs_and_junk():
    assert parse_playlist_id("liked") == "liked"
    assert parse_playlist_id("https://example.com") is None


def test_track_key_prefers_spotify_then_isrc_then_metadata():
    base = dict(title="Song", artists=["Band"], duration_ms=200_400)
    assert track_key(ImportedTrack(**base, spotify_id="abc", isrc="X")) == "abc"
    assert track_key(ImportedTrack(**base, isrc="usabc1234567")) == "isrc:USABC1234567"
    a = track_key(ImportedTrack(**base))
    assert a.startswith("meta:")
    assert a == track_key(ImportedTrack(title="SONG", artists=["band"], duration_ms=199_600))
