import time

from app_engine import links


def _parts(path):
    name = path.split("?")[0].rsplit("/", 1)[1]
    query = dict(p.split("=") for p in path.split("?")[1].split("&"))
    return name, int(query["e"]), query["s"]


def test_a_signed_link_verifies_for_its_file_only():
    name, e, s = _parts(links.signed_path("Crimsonhaven-Setup-0.2.0.exe"))
    assert links.verify(name, e, s)
    assert not links.verify("other.exe", e, s)
    assert not links.verify(name, e + 1, s)


def test_an_expired_link_fails(monkeypatch):
    name, e, s = _parts(links.signed_path("a.deb"))
    monkeypatch.setattr(time, "time", lambda: e + 1)
    assert not links.verify(name, e, s)
