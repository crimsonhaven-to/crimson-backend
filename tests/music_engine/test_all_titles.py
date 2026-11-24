"""The All titles view: scoped to the signed-in music user, deduplicated by track
id, excluding rows dropped upstream, and reusing the standard track payload."""

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from music_engine import db, payloads, routes
from music_engine.access import require_music_user


def _row(track_id: int, **overrides) -> dict:
    base = {
        "id": track_id,
        "spotify_id": f"sp{track_id}",
        "title": f"Song {track_id}",
        "artists": ["Band"],
        "album": "LP",
        "duration_ms": 180_000,
        "file_size": 3_500_000,
        "status": "ready",
        "error": None,
        "cover_url": "https://i.scdn.co/art",
        "cover_path": f"Band/LP/cover_{track_id}.jpg",
        "rel_path": f"Band/LP/{track_id}.m4a",
        "mirrored_at": None,
        "playlists": [{"id": 1, "name": "Favorites"}],
    }
    return {**base, **overrides}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_music_user] = lambda: {"user_id": 42}
    return TestClient(app)


def test_all_titles_requires_music_access():
    app = FastAPI()
    app.include_router(routes.router)
    response = TestClient(app).get("/music/all-titles")
    assert response.status_code in (401, 403)


def test_all_titles_is_scoped_to_the_calling_user(client, monkeypatch):
    seen: list[int] = []

    def fake_all_titles(user_id: int) -> list[dict]:
        seen.append(user_id)
        return [
            _row(7, playlists=[{"id": 1, "name": "Road Trip"}, {"id": 2, "name": "Liked Songs"}]),
            _row(8, status="pending", cover_path=None, file_size=None, playlists=None),
        ]

    monkeypatch.setattr(routes.store, "all_titles", fake_all_titles)
    response = client.get("/music/all-titles")
    assert response.status_code == 200
    assert seen == [42]

    body = response.json()
    assert body["success"] is True
    assert body["total"] == 2
    ready, pending = body["tracks"]

    assert ready["id"] == 7
    assert ready["stream_url"].startswith("http://testserver/music_stream/7?e=")
    assert ready["cover_url"].startswith("http://testserver/music_art/7?e=")
    assert ready["file_size"] == 3_500_000
    assert ready["removed_upstream"] is False
    assert ready["playlists"] == [
        {"id": 1, "name": "Road Trip"},
        {"id": 2, "name": "Liked Songs"},
    ]

    assert pending["id"] == 8
    assert pending["stream_url"] is None
    assert pending["file_size"] is None
    assert pending["cover_url"] == "https://i.scdn.co/art"
    assert pending["playlists"] == []


def test_all_titles_uses_cdn_links_when_mirrored(client, monkeypatch):
    monkeypatch.setattr(payloads.cdn, "enabled", lambda: True)
    monkeypatch.setattr(payloads.cdn, "signed_url", lambda rel: f"https://cdn/{rel}?sig=1")
    monkeypatch.setattr(
        routes.store,
        "all_titles",
        lambda user_id: [_row(5, mirrored_at="2026-11-01T00:00:00+00:00")],
    )
    body = client.get("/music/all-titles").json()
    track = body["tracks"][0]
    assert track["stream_url"] == "https://cdn/Band/LP/5.m4a?sig=1"
    assert track["cover_url"] == "https://cdn/Band/LP/cover_5.jpg?sig=1"


def test_all_titles_empty_library(client, monkeypatch):
    monkeypatch.setattr(routes.store, "all_titles", lambda user_id: [])
    body = client.get("/music/all-titles").json()
    assert body == {"success": True, "total": 0, "tracks": []}


def test_all_titles_query_scopes_deduplicates_and_excludes_removed(monkeypatch):
    executed: list[tuple[str, tuple]] = []

    class FakeResult:
        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=()):
            executed.append((sql, params))
            return FakeResult()

    @contextmanager
    def fake_conn():
        yield FakeConn()

    monkeypatch.setattr(db, "get_connection", fake_conn)
    assert db.store.all_titles(99) == []

    assert len(executed) == 1
    sql, params = executed[0]
    assert params == (99,)
    assert db._TRACK_COLS_T in sql
    assert "p.user_id = %s" in sql
    assert "NOT pt.removed_upstream" in sql
    assert "GROUP BY t.id" in sql
    assert "JSONB_AGG(" in sql
    assert "ORDER BY LOWER(t.title), LOWER(COALESCE(t.artists->>0, '')), t.id" in sql


def test_queue_counts_tracks_active_and_active_ready_distinctly(monkeypatch):
    rows = [
        {"status": "ready", "n": 10, "mirrored": 8, "active": 9},
        {"status": "pending", "n": 2, "mirrored": 0, "active": 2},
        {"status": "failed", "n": 1, "mirrored": 0, "active": 0},
    ]

    class FakeResult:
        def fetchall(self):
            return rows

    class FakeConn:
        def execute(self, sql, params=()):
            assert params == (7,)
            assert "NOT pt.removed_upstream" in sql
            return FakeResult()

    @contextmanager
    def fake_conn():
        yield FakeConn()

    monkeypatch.setattr(db, "get_connection", fake_conn)
    counts = db.store.queue_counts(7)
    assert counts["ready"] == 10
    assert counts["pending"] == 2
    assert counts["failed"] == 1
    assert counts["mirrored"] == 8
    assert counts["active"] == 11
    assert counts["active_ready"] == 9
