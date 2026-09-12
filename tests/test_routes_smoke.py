"""
The mounted route table: everything mounts, nothing collides, and exactly the
intended routes are reachable without signing in.

``api.py`` includes eleven engine routers plus ``web.routes.all_routers``, and an
optional overlay registers more at import time. Nothing checked that a router
actually made it onto the app, that two routers had not claimed the same path, or
which routes the login wall leaves open.

The last of those is the point of this module. The wall's whitelist is written as
paths and *prefixes*, but what matters is the set of real routes those let
through, and that set is never written down anywhere. :data:`PUBLIC_ROUTES` is
that set, pinned. Add a route under a whitelisted prefix and this test fails,
which turns "this endpoint is now public" from an accident into a decision.
"""

import warnings
from collections import Counter

import pytest

import api

# Methods every route answers by framework default; they carry no surface of
# their own and OPTIONS is deliberately never walled (see test_login_wall).
_IGNORED_METHODS = {"HEAD", "OPTIONS"}

# Every route reachable without a session, as a (method, path) pair.
#
# Each entry is here for one of five reasons:
#   * you cannot sign in without the auth endpoints,
#   * uptime probes and the service descriptor (/, /health, /config, /lumi),
#   * the API docs,
#   * a signed proxy loaded cross-origin by <video>, hls.js, <img> or <track>,
#     none of which can attach an Authorization header, so each is HMAC-signed
#     and gated by the signature instead,
#   * a caller that is not a browser: Ko-fi's webhook, Prometheus' scrape
#     (/metrics is whitelisted only so a token-carrying scrape reaches the
#     handler, which then enforces its own check and denies by default).
PUBLIC_ROUTES = {
    ("GET", "/"),
    ("GET", "/config"),
    ("GET", "/health"),
    ("GET", "/lumi"),

    ("POST", "/auth/challenge"),
    ("POST", "/auth/email/forgot"),
    ("POST", "/auth/email/login"),
    ("POST", "/auth/email/register"),
    ("POST", "/auth/email/resend"),
    ("POST", "/auth/email/reset"),
    ("POST", "/auth/email/verify"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/register"),

    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/openapi.json"),
    ("GET", "/redoc"),

    ("GET", "/cache_proxy/{token}"),
    ("GET", "/iptv_proxy"),
    ("GET", "/jellyfin_proxy/{path:path}"),
    ("POST", "/jellyfin_proxy/{path:path}"),
    ("GET", "/local_art"),
    ("GET", "/local_hls/{token}/{resource}"),
    ("GET", "/local_proxy/{token}"),
    ("GET", "/manga_proxy"),
    ("GET", "/player"),
    ("GET", "/subtitles_proxy"),

    ("GET", "/changelog"),
    ("GET", "/metrics"),
    ("POST", "/kofi/webhook"),
}


def _route_pairs(routes):
    """(method, path) for every route, minus the framework-default methods."""
    pairs = []
    for route in routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        for method in sorted(getattr(route, "methods", None) or ["GET"]):
            if method not in _IGNORED_METHODS:
                pairs.append((method, path))
    return pairs


def _is_public(path):
    return path in api._PUBLIC_EXACT or path.startswith(tuple(api._PUBLIC_PREFIXES))


ALL_ROUTERS = [
    api.account_router,
    api.admin_router,
    api.supporters_router,
    api.changelog_router,
    api.recommend_router,
    api.chat_router,
    api.subtitles_router,
    api.skiptimes_router,
    api.manga_router,
    api.iptv_router,
    *api.all_routers,
]


@pytest.mark.parametrize("router", ALL_ROUTERS, ids=lambda r: str(getattr(r, "prefix", "") or "root"))
def test_every_router_is_mounted(router):
    """A router that is defined but never included fails silently: its endpoints
    simply 404 with nothing in the log to say why."""
    mounted = set(_route_pairs(api.app.routes))
    missing = [pair for pair in _route_pairs(router.routes) if pair not in mounted]
    assert not missing, f"router routes absent from the app: {missing}"


def test_no_two_routes_claim_the_same_path_and_method():
    """Two routers claiming one path is not an error; the first registered simply
    wins and the second is dead code nobody notices."""
    duplicates = [pair for pair, n in Counter(_route_pairs(api.app.routes)).items() if n > 1]
    assert not duplicates, f"duplicate (method, path): {duplicates}"


def test_the_app_actually_has_a_route_table():
    """Guards the tests above against passing vacuously if the app failed to
    assemble and mounted almost nothing."""
    assert len(_route_pairs(api.app.routes)) > 100


def test_exactly_the_intended_routes_are_public():
    """The wall's whitelist is prefixes; this is the set of real routes those
    open. A new route under a whitelisted prefix changes this set.

    If this fails after you added a route, decide which it is: the route should
    be gated (rename it out from under the prefix), or it is genuinely public
    (add it here with its reason). Do not just paste the new value in.
    """
    actual = {pair for pair in _route_pairs(api.app.routes) if _is_public(pair[1])}

    # An operator build's overlay registers its own signed proxies and whitelists
    # them via _DYNAMIC_PUBLIC_PREFIXES. Those are the overlay's to account for,
    # and naming them here would put a private source's name in this repo.
    dynamic = tuple(api._DYNAMIC_PUBLIC_PREFIXES)
    if dynamic:
        actual = {p for p in actual if not p[1].startswith(dynamic)}

    assert actual == PUBLIC_ROUTES, (
        f"newly public: {sorted(actual - PUBLIC_ROUTES)}\n"
        f"no longer public: {sorted(PUBLIC_ROUTES - actual)}"
    )


def test_no_admin_route_is_public():
    """Belt and braces over the inventory: /admin is gated twice, by the wall and
    by require_admin on every route, and neither may be the only one."""
    for method, path in _route_pairs(api.app.routes):
        if path.startswith("/admin"):
            assert not _is_public(path), f"{method} {path} is reachable without a session"


def test_the_openapi_schema_generates():
    """Regenerating openapi.json is a documented step; a schema that cannot build
    blocks it, and the failure is otherwise only seen at release time."""
    with warnings.catch_warnings():
        # Pre-existing and cosmetic: FastAPI derives one operation id per route,
        # so the GET+POST /jellyfin_proxy route reports a collision. It affects
        # generated client naming, not routing.
        warnings.simplefilter("ignore")
        schema = api.app.openapi()
    assert schema["openapi"]
    assert len(schema["paths"]) > 100
