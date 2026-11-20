"""A card is a snapshot, and a song card needs a real share token."""

import pytest

from messaging_engine import attachments
from messaging_engine.schemas import SongAttachment, TitleAttachment
from music_engine import links


def test_a_title_keeps_only_an_https_poster():
    card = TitleAttachment(kind="title", media="anime", id=1, title=" X ", poster="javascript:alert(1)")
    assert attachments.snapshot(card) == {
        "kind": "title", "media": "anime", "id": 1, "title": "X", "poster": None,
    }


def test_a_song_is_snapshotted_from_the_library(monkeypatch):
    track = {
        "status": "ready", "title": "Song", "artists": ["Band"], "album": "LP",
        "duration_ms": 1000, "cover_url": "https://i.scdn.co/a.jpg",
    }
    monkeypatch.setattr(attachments.music_store, "get_track", lambda track_id: track if track_id == 7 else None)
    token = links.share_token(7)
    assert attachments.snapshot(SongAttachment(kind="song", token=token)) == {
        "kind": "song", "token": token, "title": "Song", "artists": ["Band"], "album": "LP",
        "duration_ms": 1000, "cover": "https://i.scdn.co/a.jpg",
    }


def test_a_forged_song_token_is_refused(monkeypatch):
    monkeypatch.setattr(attachments.music_store, "get_track", lambda track_id: {"status": "ready"})
    with pytest.raises(attachments.UnknownSong):
        attachments.snapshot(SongAttachment(kind="song", token="7.forged"))
