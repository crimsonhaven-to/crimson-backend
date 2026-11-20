"""Sharing needs the song in your own library; opening a share needs only a session."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from account_engine.deps import require_user
from music_engine import links, share_routes
from music_engine.access import require_music_user


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(share_routes.router)
    app.dependency_overrides[require_music_user] = lambda: {"user_id": 1}
    app.dependency_overrides[require_user] = lambda: {"user_id": 2}
    return TestClient(app)


def _track(status="ready"):
    return {
        "id": 7, "spotify_id": None, "title": "Song", "artists": ["Band"], "album": "LP",
        "duration_ms": 1000, "file_size": 10, "status": status, "error": None,
        "cover_url": None, "cover_path": None, "rel_path": "a/b.m4a", "mirrored_at": None,
    }


def test_only_your_own_songs_can_be_shared(client, monkeypatch):
    monkeypatch.setattr(share_routes.store, "user_has_track", lambda user_id, track_id: False)
    assert client.post("/music/tracks/7/share").status_code == 404


def test_a_shared_song_opens_for_another_member(client, monkeypatch):
    monkeypatch.setattr(share_routes.store, "user_has_track", lambda user_id, track_id: True)
    monkeypatch.setattr(share_routes.store, "get_track", lambda track_id: _track())
    shared = client.post("/music/tracks/7/share").json()
    token = shared["token"]
    assert shared["url"].endswith(f"/music/song/{token}")
    track = client.get(f"/music/shared/{token}").json()["track"]
    assert track["title"] == "Song"
    assert "/music_stream/7?" in track["stream_url"]


def test_a_forged_or_gone_share_is_404(client, monkeypatch):
    monkeypatch.setattr(share_routes.store, "get_track", lambda track_id: _track("failed"))
    assert client.get(f"/music/shared/{links.share_token(7)}").status_code == 404
    assert client.get("/music/shared/7.bad").status_code == 404
