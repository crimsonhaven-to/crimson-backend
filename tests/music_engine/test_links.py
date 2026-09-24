"""Signed media links: verify only for the same kind, track and unexpired day."""

from urllib.parse import parse_qs, urlparse

from music_engine import links


def _parts(path):
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    return parsed.path, int(query["e"][0]), query["s"][0]


def test_a_minted_link_verifies():
    path, expires, signature = _parts(links.signed_path(links.STREAM, 42))
    assert path == "/music_stream/42"
    assert links.verify(links.STREAM, 42, expires, signature)


def test_a_link_is_bound_to_its_track_and_kind():
    _path, expires, signature = _parts(links.signed_path(links.STREAM, 42))
    assert not links.verify(links.STREAM, 43, expires, signature)
    assert not links.verify(links.ART, 42, expires, signature)
    assert not links.verify(links.STREAM, 42, expires + 86400, signature)


def test_an_expired_link_fails(monkeypatch):
    _path, expires, signature = _parts(links.signed_path(links.ART, 1))
    monkeypatch.setattr(links.time, "time", lambda: expires + 1)
    assert not links.verify(links.ART, 1, expires, signature)


def test_links_are_stable_within_a_day():
    assert links.signed_path(links.STREAM, 5) == links.signed_path(links.STREAM, 5)
