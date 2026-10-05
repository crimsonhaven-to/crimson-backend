"""The members-only release routes against a fake release folder."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from account_engine.deps import require_user
from app_engine import links, routes, upstream

BASE = "https://releases.test/pkg/latest"
FILES = {
    "release.json": b'{"version": "0.2.0", "released_at": "2026-10-05", "files": ['
    b'{"name": "Crimsonhaven-Setup-0.2.0.exe", "platform": "windows", "kind": "installer", "size": 10},'
    b'{"name": "../evil", "platform": "linux", "kind": "deb", "size": 1}]}',
    "latest.yml": b"version: 0.2.0\n",
    "Crimsonhaven-Setup-0.2.0.exe": b"0123456789",
}


def _upstream(request: httpx.Request) -> httpx.Response:
    name = request.url.path.rsplit("/", 1)[1]
    if name == "broken":
        return httpx.Response(500)
    if name not in FILES:
        return httpx.Response(404)
    assert request.headers["authorization"] == "Bearer secret"
    return httpx.Response(200, content=FILES[name])


@pytest.fixture
def make_client(monkeypatch):
    def make(*, url=BASE, signed_in=True):
        monkeypatch.setenv("APP_RELEASES_URL", url)
        monkeypatch.setenv("APP_RELEASES_TOKEN", "secret")
        monkeypatch.setattr(upstream, "transport", httpx.MockTransport(_upstream))
        upstream.forget_manifest()
        app = FastAPI()
        app.include_router(routes.router)
        if signed_in:
            app.dependency_overrides[require_user] = lambda: {"id": 1}
        return TestClient(app)

    return make


def test_release_lists_files_with_signed_links(make_client):
    body = make_client().get("/app/release").json()
    assert body["version"] == "0.2.0"
    [only] = body["files"]
    assert only["name"] == "Crimsonhaven-Setup-0.2.0.exe"
    assert only["url"].startswith("/app/download/Crimsonhaven-Setup-0.2.0.exe?e=")


def test_members_only(make_client):
    client = make_client(signed_in=False)
    assert client.get("/app/release").status_code == 401
    assert client.get("/app/updates/latest.yml").status_code == 401


def test_unconfigured_is_503(make_client):
    client = make_client(url="")
    assert client.get("/app/release").status_code == 503
    assert client.get("/app/updates/latest.yml").status_code == 503


def test_update_feed_streams_files_and_passes_404(make_client):
    client = make_client()
    response = client.get("/app/updates/latest.yml")
    assert response.status_code == 200
    assert response.content == b"version: 0.2.0\n"
    assert response.headers["content-length"] == "15"
    assert client.get("/app/updates/missing.yml").status_code == 404
    assert client.get("/app/updates/broken").status_code == 502


def test_invalid_names_never_reach_upstream(make_client):
    client = make_client()
    assert client.get("/app/updates/.hidden").status_code == 404
    assert client.get("/app/updates/a%20b").status_code == 404


def test_signed_download_is_an_attachment(make_client):
    client = make_client(signed_in=False)
    response = client.get(links.signed_path("Crimsonhaven-Setup-0.2.0.exe"))
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert 'filename="Crimsonhaven-Setup-0.2.0.exe"' in response.headers["content-disposition"]


def test_a_bad_signature_is_404(make_client):
    client = make_client(signed_in=False)
    path = links.signed_path("Crimsonhaven-Setup-0.2.0.exe")
    assert client.get(path[:-1] + ("0" if path[-1] != "0" else "1")).status_code == 404
