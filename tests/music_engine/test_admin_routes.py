"""The admin library view: admins only, and its filters are validated."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from account_engine.deps import require_admin
from music_engine import admin_routes


def _client(monkeypatch, admin=True):
    app = FastAPI()
    app.include_router(admin_routes.router)
    if admin:
        app.dependency_overrides[require_admin] = lambda: {"user_id": 1, "is_admin": True}
    asked = {}
    monkeypatch.setattr(admin_routes.store, "library_summary", lambda: {"total": 1, "ready": 1})

    def page(**kwargs):
        asked.update(kwargs)
        row = {"id": 7, "spotify_id": None, "title": "Song", "artists": ["Band"], "album": "",
               "duration_ms": 1000, "status": "ready", "error": None, "cover_path": None,
               "cover_url": None, "rel_path": "Band/Singles/Song.m4a", "mirrored_at": None,
               "file_size": 1234, "created_at": "2026-09-24", "owners": ["ray"],
               "playlist_count": 2}
        return [row], 1

    monkeypatch.setattr(admin_routes.store, "library_page", page)
    return TestClient(app), asked


def test_lists_songs_with_owners_and_a_preview_link(monkeypatch):
    client, asked = _client(monkeypatch)
    body = client.get("/admin/music/library?q=%20band%20&status=ready&limit=10").json()
    assert asked == {"query": "band", "status": "ready", "limit": 10, "offset": 0}
    track = body["tracks"][0]
    assert (track["owners"], track["playlist_count"], track["mirrored"]) == (["ray"], 2, False)
    assert track["stream_url"].startswith("http://testserver/music_stream/7?e=")
    assert body["summary"]["cdn"] is False


def test_an_unknown_status_is_refused(monkeypatch):
    client, _ = _client(monkeypatch)
    assert client.get("/admin/music/library?status=gone").status_code == 422


def test_admins_only(monkeypatch):
    client, _ = _client(monkeypatch, admin=False)
    assert client.get("/admin/music/library").status_code in (401, 403)
