"""What the player gets for a track: signed links to the api's copy, or to the
CDN copy once the song is there."""

from music_engine import payloads


def test_a_ready_track_gets_signed_stream_and_art_urls():
    row = {"id": 3, "spotify_id": None, "title": "t", "artists": ["a"], "album": "",
           "duration_ms": 1, "status": "ready", "error": None, "cover_path": "a/cover.jpg",
           "cover_url": "https://i.scdn.co/x", "file_size": 4_000_000}
    payload = payloads.track_payload(row, "https://api")
    assert payload["stream_url"].startswith("https://api/music_stream/3?e=")
    assert payload["cover_url"].startswith("https://api/music_art/3?e=")
    assert payload["file_size"] == 4_000_000
    pending = payloads.track_payload({**row, "status": "pending", "cover_path": None}, "https://api")
    assert pending["stream_url"] is None
    assert pending["cover_url"] == "https://i.scdn.co/x"
    assert pending["file_size"] is None


def test_a_mirrored_track_streams_from_the_cdn(monkeypatch):
    monkeypatch.setattr(payloads.cdn, "enabled", lambda: True)
    monkeypatch.setattr(payloads.cdn, "signed_url", lambda rel: f"https://cdn/{rel}?signed")
    row = {"id": 3, "spotify_id": None, "title": "t", "artists": ["a"], "album": "",
           "duration_ms": 1, "status": "ready", "error": None, "cover_path": "a/cover.jpg",
           "cover_url": None, "rel_path": "a/t.m4a", "mirrored_at": "2026-09-24",
           "file_size": 1}
    payload = payloads.track_payload(row, "https://api")
    assert payload["stream_url"] == "https://cdn/a/t.m4a?signed"
    assert payload["cover_url"] == "https://cdn/a/cover.jpg?signed"
    local = payloads.track_payload({**row, "mirrored_at": None}, "https://api")
    assert local["stream_url"].startswith("https://api/music_stream/3?e=")
