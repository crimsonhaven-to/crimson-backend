"""The listening API: reports go through the plausibility rules, and Wrapped
hands top songs back as playable tracks."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from music_engine import listening_routes
from music_engine.access import require_music_user


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(listening_routes.router)
    app.dependency_overrides[require_music_user] = lambda: {"user_id": 3}
    return TestClient(app)


def test_reports_are_recorded_for_the_signed_in_account(client, monkeypatch):
    seen = []
    monkeypatch.setattr(listening_routes.listens, "record",
                        lambda user_id, reported: seen.append((user_id, reported)) or 1)
    body = {"listens": [{"track_id": 4, "listened_at": "2026-09-30T10:00:00Z", "seconds": 95}]}
    response = client.post("/music/listens", json=body)
    assert response.status_code == 200
    assert response.json()["recorded"] == 1
    [(user_id, [(track_id, moment, seconds)])] = seen
    assert (user_id, track_id, seconds) == (3, 4, 95.0)
    assert moment == datetime(2026, 9, 30, 10, tzinfo=timezone.utc)


def test_a_huge_batch_is_refused(client):
    one = {"track_id": 4, "listened_at": "2026-09-30T10:00:00Z", "seconds": 95}
    assert client.post("/music/listens", json={"listens": [one] * 201}).status_code == 422


def test_wrapped_returns_top_songs_as_tracks(client, monkeypatch):
    row = {"id": 4, "spotify_id": None, "title": "Song", "artists": ["Band"], "album": "",
           "duration_ms": 1000, "file_size": 10, "status": "ready", "error": None,
           "rel_path": "a.m4a", "cover_path": None, "cover_url": None, "mirrored_at": None}
    stats = {"plays": 2, "minutes": 3, "songs": 1, "artists": 1, "active_days": 1,
             "top_tracks": [{"row": row, "plays": 2, "minutes": 3}], "top_artists": []}
    monkeypatch.setattr(listening_routes.wrapped, "build", lambda u, y, o: stats)
    response = client.get("/music/wrapped?year=2026&offset_minutes=120")
    assert response.status_code == 200
    [top] = response.json()["top_tracks"]
    assert top["plays"] == 2
    assert top["track"]["title"] == "Song"
    assert "stream_url" in top["track"]


def test_wrapped_refuses_an_offset_no_timezone_has(client):
    assert client.get("/music/wrapped?offset_minutes=5000").status_code == 422
