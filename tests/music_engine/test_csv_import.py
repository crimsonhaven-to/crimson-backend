"""CSV imports: Exportify's old and new columns, and other exporters' names."""

import pytest

from music_engine.csv_import import CsvImportError, parse_csv, spotify_track_id

EXPORTIFY_OLD = (
    '"Track URI","Track Name","Artist Name(s)","Album Name","Album Artist Name(s)",'
    '"Album Release Date","Album Image URL","Disc Number","Track Number",'
    '"Track Duration (ms)","ISRC","Added At"\n'
    '"spotify:track:2FZcjBYK4dTt48q94pJbJD","Bass Persuades","Miley Cyrus,Dolly Parton",'
    '"Something Beautiful","Miley Cyrus","2026-09-03","https://i.scdn.co/image/x","1","3",'
    '"202460","USSM12600001","2026-09-10T10:00:00Z"\n'
)

EXPORTIFY_NEW = (
    "﻿Track URI,Track Name,Album Name,Artist Name(s),Release Date,Duration (ms)\n"
    "spotify:track:2FZcjBYK4dTt48q94pJbJD,Bass Persuades,Something Beautiful,"
    "Miley Cyrus;Dolly Parton,2026-09-03,202460\n"
)


def test_old_exportify_columns_fill_every_field():
    [track] = parse_csv(EXPORTIFY_OLD)
    assert track.spotify_id == "2FZcjBYK4dTt48q94pJbJD"
    assert track.artists == ["Miley Cyrus", "Dolly Parton"]
    assert track.album == "Something Beautiful"
    assert track.album_artist == "Miley Cyrus"
    assert (track.disc_number, track.track_number) == (1, 3)
    assert track.duration_ms == 202460
    assert track.isrc == "USSM12600001"
    assert track.cover_url == "https://i.scdn.co/image/x"


def test_new_exportify_columns_and_semicolon_artists():
    [track] = parse_csv(EXPORTIFY_NEW)
    assert track.artists == ["Miley Cyrus", "Dolly Parton"]
    assert track.album_artist == "Miley Cyrus"
    assert track.release_date == "2026-09-03"


def test_an_exporter_without_ids_or_milliseconds():
    [track] = parse_csv("Track name,Artist name,Album,Duration\nSong,Band,Record,3:25\n")
    assert track.spotify_id is None
    assert track.duration_ms == 205_000


def test_rows_without_title_or_artist_are_skipped():
    tracks = parse_csv("Track Name,Artist Name(s)\nSong,Band\n,Band\nSong,\n")
    assert [t.title for t in tracks] == ["Song"]


def test_missing_columns_explain_themselves():
    with pytest.raises(CsvImportError, match="track name and an artist"):
        parse_csv("Foo,Bar\n1,2\n")


def test_a_file_with_no_usable_rows_is_refused():
    with pytest.raises(CsvImportError):
        parse_csv("Track Name,Artist Name(s)\n,\n")


@pytest.mark.parametrize(
    "value",
    [
        "spotify:track:2FZcjBYK4dTt48q94pJbJD",
        "https://open.spotify.com/track/2FZcjBYK4dTt48q94pJbJD?si=abc",
        "2FZcjBYK4dTt48q94pJbJD",
    ],
)
def test_spotify_track_ids_in_every_spelling(value):
    assert spotify_track_id(value) == "2FZcjBYK4dTt48q94pJbJD"


def test_no_track_id_in_junk():
    assert spotify_track_id("") is None
    assert spotify_track_id("not an id") is None
