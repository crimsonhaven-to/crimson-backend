"""The music migration keeps the grant deny by default and never deletes audio
when a playlist or a Spotify entry goes away."""

import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[2] / "migrations" / "008_music.sql").read_text()


def test_music_is_deny_by_default():
    assert re.search(r"music_enabled\s+BOOLEAN\s+NOT NULL\s+DEFAULT FALSE", SQL)


def test_tracks_outlive_their_playlists():
    tracks = SQL[SQL.index("CREATE TABLE IF NOT EXISTS music_tracks"):]
    tracks = tracks[: tracks.index(");")]
    assert "REFERENCES" not in tracks
    assert "removed_upstream" in SQL
