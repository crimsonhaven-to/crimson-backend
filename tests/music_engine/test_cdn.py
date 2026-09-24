"""Links to the CDN copy: the key is the share path, and the signature is the
one the music-cdn Worker checks."""

import pytest

from core.config import get_settings
from music_engine import cdn


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("MUSIC_CDN_URL", "https://cdn.example/")
    monkeypatch.setenv("MUSIC_CDN_SECRET", "s3cret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_off_unless_both_are_set(monkeypatch):
    monkeypatch.setenv("MUSIC_CDN_URL", "https://cdn.example")
    monkeypatch.delenv("MUSIC_CDN_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        assert not cdn.enabled()
    finally:
        get_settings.cache_clear()


def test_a_signed_url_quotes_the_key_and_verifies(configured):
    url = cdn.signed_url("Fleetwood Mac/Rumours (Deluxe)/02 - Dreams.m4a")
    assert url.startswith("https://cdn.example/Fleetwood%20Mac/Rumours%20%28Deluxe%29/02%20-%20Dreams.m4a?e=")
    query = dict(part.split("=") for part in url.split("?", 1)[1].split("&"))
    key = "Fleetwood Mac/Rumours (Deluxe)/02 - Dreams.m4a"
    assert cdn.verify(key, int(query["e"]), query["s"])
    assert not cdn.verify("Other/key.m4a", int(query["e"]), query["s"])
    assert not cdn.verify(key, 1000, query["s"])


def test_the_worker_signs_the_same_payload(configured):
    """deploy/music-cdn/src/index.js signs `music-cdn:${key}:${expires}` and
    keeps 32 hex characters, like core.signing."""
    source = open("deploy/music-cdn/src/index.js").read()
    assert "`music-cdn:${key}:${expires}`" in source
    assert ".slice(0, 32)" in source
    assert cdn._payload("a/b.m4a", 5) == "music-cdn:a/b.m4a:5"
