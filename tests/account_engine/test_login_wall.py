"""What the login wall lets through and what it stops.

Most tests drive the middleware over a stub app, so a 200 means exactly "the wall
passed it through" and nothing touches the database. The last few use the real
app, to prove the wall is mounted and ordered correctly. Lifespan never runs:
httpx.ASGITransport does not start it.
"""

import httpx
import pytest

import api
from account_engine import login_wall
from core.config import get_settings

PUBLIC_EXACT = sorted(login_wall.PUBLIC_EXACT)
# Plus the overlay's relays, which an operator build registers at import.
PUBLIC_PREFIXES = login_wall.PUBLIC_PREFIXES + api.overlay_prefixes

# Representative of the surfaces the wall exists to protect: discovery, playback,
# accounts, admin and the engine routers.
GATED_PATHS = [
    "/trending",
    "/catalogue",
    "/search/anime",
    "/watch/1/1/1",
    "/info/1",
    "/account/me",
    "/account/progress",
    "/users",
    "/recommendations",
    "/supporters",
    "/skiptimes",
    "/subtitles",
]


async def _stub_app(scope, receive, send):
    """Stands in for the real app: 200 on anything the wall lets through."""
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"reached"})


def _client(app=None):
    """An async client wired straight to the wall (over the stub by default)."""
    wall = login_wall.LoginWallMiddleware(app or _stub_app)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wall), base_url="http://testserver")


def _no_valid_credentials(monkeypatch):
    """Reject every token and key, so only the whitelist can let a request through."""
    async def _reject(_value):
        return False
    monkeypatch.setattr(login_wall, "session_is_valid", _reject)
    monkeypatch.setattr(login_wall, "apikey_is_valid", _reject)


# --- the whitelist ----------------------------------------------------------

@pytest.mark.parametrize("path", PUBLIC_EXACT)
async def test_public_exact_paths_pass_without_a_bearer(path, monkeypatch):
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        assert (await client.get(path)).status_code == 200


@pytest.mark.parametrize("prefix", PUBLIC_PREFIXES)
async def test_public_prefixes_pass_without_a_bearer(prefix, monkeypatch):
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        assert (await client.get(prefix)).status_code == 200
        # A child path is the case that actually matters: these are stream
        # proxies and token paths, never bare prefixes.
        child = prefix.rstrip("/") + "/child-resource"
        assert (await client.get(child)).status_code == 200


@pytest.mark.parametrize("path", GATED_PATHS)
async def test_gated_paths_are_denied_without_a_bearer(path, monkeypatch):
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        response = await client.get(path)

    assert response.status_code == 401
    body = response.json()
    # The client branches on `success` and renders `message`, so the shape is
    # part of the contract, not just the status code.
    assert body["success"] is False
    assert body["detail"] == "Authentication required"
    assert isinstance(body["message"], str) and body["message"]


async def test_no_gated_path_is_accidentally_whitelisted():
    """The two collections must not overlap the surfaces they exist to protect."""
    for path in GATED_PATHS:
        assert path not in login_wall.PUBLIC_EXACT
        assert not path.startswith(PUBLIC_PREFIXES)


# --- credentials ------------------------------------------------------------

async def test_a_valid_session_unlocks_a_gated_path(monkeypatch):
    async def _accept(_token):
        return True
    monkeypatch.setattr(login_wall, "session_is_valid", _accept)

    async with _client() as client:
        response = await client.get("/trending", headers={"Authorization": "Bearer good-token"})
    assert response.status_code == 200


async def test_a_malformed_authorization_header_is_not_a_session(monkeypatch):
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        for header in ("good-token", "Basic good-token", "Bearer", ""):
            response = await client.get("/trending", headers={"Authorization": header})
            assert response.status_code == 401, header


async def test_the_bearer_scheme_is_case_insensitive(monkeypatch):
    seen = []

    async def _accept(token):
        seen.append(token)
        return True
    monkeypatch.setattr(login_wall, "session_is_valid", _accept)

    async with _client() as client:
        response = await client.get("/trending", headers={"Authorization": "bEaReR tok"})
    assert response.status_code == 200
    assert seen == ["tok"]


# --- the bridge API key, which is scoped to /mw alone -----------------------

async def test_api_key_unlocks_the_mw_bridge(monkeypatch):
    async def _reject_sessions(_token):
        return False

    async def _accept_keys(_key):
        return True
    monkeypatch.setattr(login_wall, "session_is_valid", _reject_sessions)
    monkeypatch.setattr(login_wall, "apikey_is_valid", _accept_keys)

    async with _client() as client:
        for path in ("/mw", "/mw/watch/movie/1", "/mw/watch/1/1/1"):
            response = await client.get(path, headers={"X-API-Key": "key"})
            assert response.status_code == 200, path


async def test_api_key_is_not_a_skeleton_key(monkeypatch):
    """A key handed to the movie-web bridge must not open the rest of the API."""
    async def _reject_sessions(_token):
        return False

    async def _accept_keys(_key):
        return True
    monkeypatch.setattr(login_wall, "session_is_valid", _reject_sessions)
    monkeypatch.setattr(login_wall, "apikey_is_valid", _accept_keys)

    async with _client() as client:
        for path in GATED_PATHS + ["/mwatch", "/mw-admin"]:
            response = await client.get(path, headers={"X-API-Key": "key"})
            assert response.status_code == 401, path


async def test_an_invalid_api_key_does_not_open_the_bridge(monkeypatch):
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        response = await client.get("/mw/watch/1/1/1", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


# --- the escape hatches -----------------------------------------------------

async def test_options_is_never_walled(monkeypatch):
    """CORS preflight carries no Authorization header, so walling it would turn
    every cross-origin call into an opaque browser failure."""
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        for path in GATED_PATHS:
            assert (await client.options(path)).status_code == 200, path


async def test_require_login_false_disables_the_wall(monkeypatch):
    _no_valid_credentials(monkeypatch)
    monkeypatch.setattr(get_settings(), "require_login", False)
    async with _client() as client:
        for path in GATED_PATHS:
            assert (await client.get(path)).status_code == 200, path


async def test_the_wall_is_read_per_request_not_at_import(monkeypatch):
    """REQUIRE_LOGIN is consulted inside __call__, which is what lets the setting
    be changed without rebuilding the app."""
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        assert (await client.get("/trending")).status_code == 401
        monkeypatch.setattr(get_settings(), "require_login", False)
        assert (await client.get("/trending")).status_code == 200
        monkeypatch.setattr(get_settings(), "require_login", True)
        assert (await client.get("/trending")).status_code == 401


# --- a documented surprise --------------------------------------------------

async def test_prefix_matching_is_not_segment_aware(monkeypatch):
    """``path.startswith(PUBLIC_PREFIXES)`` is a string prefix test, not a path
    segment test, so ``/player`` also opens ``/players`` and ``/local_art`` also
    opens ``/local_artifacts``.

    This asserts the behaviour as it is today rather than the behaviour one might
    assume. It is latent, not live: nothing is mounted at any of these names, so
    a request reaches a 404 rather than a handler. It matters the day someone
    adds a route whose path merely starts with a whitelisted prefix, because it
    would be public without anyone deciding that. Tightening the match to a
    segment boundary is a behaviour change and belongs in its own change, not
    smuggled in under a test.
    """
    _no_valid_credentials(monkeypatch)
    async with _client() as client:
        for path in ("/players", "/playerx", "/local_artifacts", "/changelogs"):
            assert (await client.get(path)).status_code == 200, path


# --- the real app: mounting and ordering ------------------------------------
# These drive api.app itself. Every path used here is denied by the wall before
# any handler runs, so no route body executes and nothing reaches the database.

async def test_the_wall_is_actually_mounted_on_the_app(monkeypatch):
    _no_valid_credentials(monkeypatch)
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/trending")
    assert response.status_code == 401
    assert response.json()["success"] is False


async def test_cors_headers_reach_the_401(monkeypatch):
    """CORS is added after the wall so it ends up outermost, which is what lets a
    browser read the 401 instead of reporting an opaque CORS failure. Reorder the
    two and this is how you find out."""
    _no_valid_credentials(monkeypatch)
    origin = get_settings().allowed_origins[0]
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/trending", headers={"Origin": origin})

    assert response.status_code == 401
    assert response.headers.get("access-control-allow-origin") == origin
