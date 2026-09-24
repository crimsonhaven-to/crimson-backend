"""The site-wide login wall: everything is private unless listed here.

Pure ASGI rather than BaseHTTPMiddleware, so it adds no buffering to the
progressive /watch stream: it reads the request scope, then either answers 401
or passes the channels straight through.
"""

import asyncio
import hashlib
import time
from typing import Callable, Dict

from fastapi.responses import JSONResponse

from apikey_engine.db import store as apikey_store
from core import lumi
from core.config import get_settings

from .db import store

PUBLIC_EXACT = {
    "/",
    "/lumi",
    "/health",
    "/config",
    "/metrics",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}
# /metrics is listed only so a scrape carrying METRICS_TOKEN reaches its handler,
# which does its own token-or-admin check. The media relays below are loaded by
# <video>, <audio>, <img>, <track> and hls.js, none of which can attach a bearer, so each
# is signed or maps a path token to a file inside an enabled root instead. A
# working URL for any of them only comes from an authenticated call.
PUBLIC_PREFIXES = (
    "/auth/",
    "/kofi/webhook",
    "/changelog",
    "/player",
    "/jellyfin_proxy",
    "/cache_proxy",
    "/local_proxy",
    "/local_hls",
    "/local_art",
    "/subtitles_proxy",
    "/manga_proxy",
    "/iptv_proxy",
    "/music_stream",
    "/music_art",
)


class _ValidTokenCache:
    """Keeps the wall off the database for every content request. Keyed by the
    token's SHA-256, never the token, and short-lived so a logout lands within
    the TTL."""

    def __init__(self, validate: Callable[[str], bool], max_entries: int, ttl: float = 60.0):
        self._validate = validate
        self._max = max_entries
        self._ttl = ttl
        self._until: Dict[str, float] = {}

    async def __call__(self, raw: str) -> bool:
        if not raw:
            return False
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now = time.monotonic()
        if self._until.get(key, 0) > now:
            return True
        # A miss validates off the event loop; the same statement stamps
        # last-seen, so that write happens at most once per token per TTL.
        if not await asyncio.to_thread(self._validate, raw):
            self._until.pop(key, None)
            return False
        if len(self._until) >= self._max:
            self._until.clear()
        self._until[key] = now + self._ttl
        return True


session_is_valid = _ValidTokenCache(store.validate_and_touch_session, max_entries=20_000)
apikey_is_valid = _ValidTokenCache(apikey_store.validate_and_touch, max_entries=5_000)


def _is_public(path: str, extra_prefixes: tuple) -> bool:
    return (
        path in PUBLIC_EXACT
        or path.startswith(PUBLIC_PREFIXES)
        or bool(extra_prefixes and path.startswith(extra_prefixes))
    )


class LoginWallMiddleware:
    """``extra_public_prefixes`` are the overlay's relays, known only once the app
    has registered them."""

    def __init__(self, app, extra_public_prefixes: tuple = ()):
        self.app = app
        self.extra_public_prefixes = extra_public_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not get_settings().require_login:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if scope.get("method") == "OPTIONS" or _is_public(path, self.extra_public_prefixes):
            return await self.app(scope, receive, send)

        token = api_key = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value.decode("latin-1")
                if header[:7].lower() == "bearer ":
                    token = header.split(" ", 1)[1].strip()
            elif name == b"x-api-key":
                api_key = value.decode("latin-1").strip()

        if token and await session_is_valid(token):
            return await self.app(scope, receive, send)
        # A key unlocks the movie-web bridge and nothing else, so handing one to
        # the fork does not make it a skeleton key for the whole backend.
        if (
            (path == "/mw" or path.startswith("/mw/"))
            and api_key
            and await apikey_is_valid(api_key)
        ):
            return await self.app(scope, receive, send)

        response = JSONResponse(
            {
                "detail": "Authentication required",
                "message": lumi.voiced_error(401),
                "success": False,
            },
            status_code=401,
        )
        await response(scope, receive, send)
