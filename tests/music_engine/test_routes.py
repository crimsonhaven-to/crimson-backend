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


def test_a_song_is_added_to_a_local_playlist(client, monkeypatch):
    monkeypatch.setattr(routes.store, "get_playlist",
                        lambda user_id, playlist_id: {"id": playlist_id, "source": "local"})
    added = []

    async def fake_add(playlist, url, song):
        added.append((url, song.title, song.artists))
        return {"track_id": 4, "added": True}

    monkeypatch.setattr(routes.library, "add_song", fake_add)
    body = {"url": "https://youtu.be/x", "title": "Band - Song (Official Video)",
            "channel": "BandVEVO", "duration_ms": 1000}
    response = client.post("/music/playlists/2/tracks", json=body)
    assert response.status_code == 200
    assert added == [("https://youtu.be/x", "Song", ["Band"])]
    body["url"] = "http://youtu.be/x"
    assert client.post("/music/playlists/2/tracks", json=body).status_code == 422


def test_songs_cannot_be_added_to_an_imported_playlist(client, monkeypatch):
    monkeypatch.setattr(routes.store, "get_playlist",
                        lambda user_id, playlist_id: {"id": playlist_id, "source": "spotify"})
    body = {"url": "https://youtu.be/x", "title": "Song"}
    assert client.post("/music/playlists/2/tracks", json=body).status_code == 400
    assert client.delete("/music/playlists/2/tracks/4").status_code == 400
