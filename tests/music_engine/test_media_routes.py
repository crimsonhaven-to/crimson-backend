"""The public audio route: a good signature serves the file with Range, anything
else is a 404 that says nothing about why."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from music_engine import links, media_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path))
    (tmp_path / "Band").mkdir()
    (tmp_path / "Band" / "song.m4a").write_bytes(b"0123456789")
    tracks = {
        1: {"status": "ready", "rel_path": "Band/song.m4a", "cover_path": None},
        2: {"status": "pending", "rel_path": None, "cover_path": None},
        3: {"status": "ready", "rel_path": "../../etc/passwd", "cover_path": None},
    }
    monkeypatch.setattr(media_routes.store, "get_track", lambda track_id: tracks.get(track_id))
    app = FastAPI()
    app.include_router(media_routes.router)
    return TestClient(app)


def test_a_signed_link_serves_ranges(client):
    response = client.get(links.signed_path(links.STREAM, 1), headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-type"] == "audio/mp4"


def test_a_bad_signature_is_404(client):
    path = links.signed_path(links.STREAM, 1)
    assert client.get(path[:-1] + ("0" if path[-1] != "0" else "1")).status_code == 404


def test_an_art_link_cannot_stream(client):
    art = links.signed_path(links.ART, 1)
    assert client.get(art.replace("music_art", "music_stream")).status_code == 404


def test_unready_and_escaping_tracks_are_404(client):
    assert client.get(links.signed_path(links.STREAM, 2)).status_code == 404
    assert client.get(links.signed_path(links.STREAM, 3)).status_code == 404
