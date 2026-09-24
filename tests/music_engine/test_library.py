"""Playlist ids from whatever a user pastes, and how tracks are keyed."""

import pytest

from music_engine.library import (
    LibraryError,
    add_song,
    parse_playlist_id,
    remove_song,
    search_key,
    song_from_search,
    track_key,
)
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


@pytest.mark.parametrize(
    "title, channel, want_title, want_artists",
    [
        ("Song", "Band - Topic", "Song", ["Band"]),
        ("Band - Song (Official Music Video)", "BandVEVO", "Song", ["Band"]),
        ("Song [Lyrics]", "Some Channel", "Song", ["Some Channel"]),
        ("Live Audio Session", "Band", "Live Audio Session", ["Band"]),
        ("A - B - C", "Band - Topic", "A - B - C", ["Band"]),
    ],
)
def test_song_from_search_guesses_title_and_artist(title, channel, want_title, want_artists):
    song = song_from_search(title, channel, 1000, "")
    assert (song.title, song.artists, song.cover_url) == (want_title, want_artists, None)


def test_search_key_is_stable_per_url():
    assert search_key("https://youtu.be/a") == search_key("https://youtu.be/a")
    assert search_key("https://youtu.be/a") != search_key("https://youtu.be/b")
    assert search_key("https://youtu.be/a").startswith("url:")


async def test_imported_playlists_cannot_be_edited():
    with pytest.raises(LibraryError):
        await add_song({"id": 1, "source": "spotify"}, "https://youtu.be/a",
                       song_from_search("t", "c", 0, ""))
    with pytest.raises(LibraryError):
        await remove_song({"id": 1, "source": "csv"}, 5)
