"""The music API's error contract: a dead Spotify link is never a 401, because
the client treats a 401 as its own session ending."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from music_engine import routes
from music_engine.access import require_music_user
from music_engine.csv_import import CsvImportError
from music_engine.provider import ProviderError
from music_engine.spotify import SpotifyAuthError, SpotifyError


@pytest.mark.parametrize(
    "error, status",
    [
        (SpotifyAuthError("dead"), 409),
        (SpotifyError("down", 500), 502),
        (ProviderError("gone"), 502),
        (CsvImportError("bad"), 400),
        (routes.library.LibraryError("no"), 400),
    ],
)
def test_errors_map_to_statuses(error, status):
    assert routes._error(error).status_code == status


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_music_user] = lambda: {"user_id": 1}
    return TestClient(app)


def test_a_track_outside_your_playlists_is_404(client, monkeypatch):
    monkeypatch.setattr(routes.store, "user_has_track", lambda user_id, track_id: False)
    assert client.post("/music/tracks/5/retry").status_code == 404


def test_a_manual_match_must_be_https(client, monkeypatch):
    monkeypatch.setattr(routes.store, "user_has_track", lambda user_id, track_id: True)
    monkeypatch.setattr(routes.store, "get_track", lambda track_id: {"id": track_id})
    chosen = []
    monkeypatch.setattr(routes.store, "choose_match", lambda track_id, url: chosen.append(url))
    assert client.post("/music/tracks/5/match", json={"url": "file:///etc/passwd"}).status_code == 422
    assert client.post("/music/tracks/5/match", json={"url": "https://youtu.be/x"}).status_code == 200
    assert chosen == ["https://youtu.be/x"]


def test_a_bad_csv_is_a_400_with_the_reason(client):
    response = client.post("/music/playlists/csv", json={"name": "x", "csv": "Foo\n1\n"})
    assert response.status_code == 400
    assert "track name" in response.json()["detail"]


def test_a_ready_track_gets_signed_stream_and_art_urls():
    row = {"id": 3, "spotify_id": None, "title": "t", "artists": ["a"], "album": "",
           "duration_ms": 1, "status": "ready", "error": None, "cover_path": "a/cover.jpg",
           "cover_url": "https://i.scdn.co/x"}
    payload = routes._track_payload(row, "https://api")
    assert payload["stream_url"].startswith("https://api/music_stream/3?e=")
    assert payload["cover_url"].startswith("https://api/music_art/3?e=")
    pending = routes._track_payload({**row, "status": "pending", "cover_path": None}, "https://api")
    assert pending["stream_url"] is None
    assert pending["cover_url"] == "https://i.scdn.co/x"
