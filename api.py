"""The FastAPI assembler: the "brain, not pipe" entrypoint.

Creates the app, mounts the middleware (login wall, CORS, Lumi header), registers
the optional overlay's stream proxies, wires the exception handlers and includes
the routers.

Three things this file deliberately does not hold: the endpoints and their logic
live in the ``web`` package (see web/__init__.py), the lifespan body lives in
``startup.py``, and the engines own their own routers.
"""

import os
import hashlib
import importlib
import inspect
import pkgutil
import logging
from typing import Dict
from contextlib import asynccontextmanager
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse
from fastapi.requests import Request
from dotenv import load_dotenv
from starlette.concurrency import run_in_threadpool

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.rate_limit import limiter
from core import lumi
from core.version import VERSION
from core.config import Config
from core import logging_setup
from core import observability
from core.http_client import (
    open_client as open_http_client,
)

from account_engine import router as account_router, store as account_store
from account_engine import audit as security_audit
from account_engine.routes import set_episode_enricher, set_warmup_handler
from account_engine.admin_routes import (
    router as admin_router,
    set_resync_handler,
    set_system_handler,
    set_source_health_handler,
)
from apikey_engine import store as apikey_store
from supporters_engine import router as supporters_router
from changelog_engine import router as changelog_router
from recommend_engine import router as recommend_router
from chat_engine import router as chat_router
from subtitles_engine import router as subtitles_router
from skiptimes_engine import router as skiptimes_router
from manga_engine import manga_router
from iptv_engine import (
    router as iptv_router,
)

# The HTTP layer: singletons, injected engine handlers and routers.
from web.pipeline import _enrich_progress_rows
from web.warmup import schedule_warmup
from web.admin_handlers import admin_source_health, admin_system_info, forced_resync
from web.routes import all_routers
from web.routes.proxies import _proxy_response

# Everything lifespan does: schema, migrations, background jobs, workers, drain.
import startup

# Same output as a bare basicConfig, plus the per-request correlation id and an
# opt-in JSON format. See core/logging_setup.py.
logging_setup.configure(level=logging.INFO)
logger = logging.getLogger(__name__)


# Defensive: core.config already loads its own.
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the schedulers, databases and warm caches; drain them on shutdown.

    The body lives in ``startup.py``, which is also where the rule for which
    replica runs which background job is written down."""
    logger.info("Starting up FastAPI application...")

    startup.report_config(logger)
    # Kept warm for the whole process lifetime; startup.shutdown closes it.
    open_http_client()
    startup.init_schema(logger)
    startup.install_observability(logger)
    startup.bootstrap_admins(logger)
    # On the loop thread, because the warm-ups it fires use asyncio.create_task.
    app.state.scheduler = startup.start_scheduler(logger)
    await startup.start_workers(logger)

    yield

    await startup.shutdown(app, logger)


# --- APP --------------------------------------------------------------------
app = FastAPI(
    title="Anime Streaming API",
    description="API for streaming anime with multi-season support",
    version=VERSION,
    lifespan=lifespan,
    # Several times faster than stdlib json on every plain `return {...}`. The
    # hand-rolled streaming and gzip responses build their own Response objects
    # and are unaffected.
    default_response_class=ORJSONResponse,
)

# Registered on app.state so the @limiter.limit decorators take effect. The 429
# handler returns a clean JSON error with Retry-After.
app.state.limiter = limiter


async def _voiced_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """slowapi's 429 in Lumi's voice. Delegates to the original for the status and
    ``Retry-After``, then re-skins the body."""
    # A tripped limiter is the strongest flood signal available, so it goes in the
    # security ledger.
    security_audit.log_event(
        "rate_limited", outcome="failure", request=request,
        detail={"path": request.url.path},
    )
    base = _rate_limit_exceeded_handler(request, exc)
    retry_after = {
        k: v for k, v in base.headers.items() if k.lower() == "retry-after"
    }
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "error": "Rate limit exceeded",
            "message": lumi.voiced_error(429),
            "status_code": 429,
        },
        headers=retry_after or None,
    )


app.add_exception_handler(RateLimitExceeded, _voiced_rate_limit_handler)

# --- SITE-WIDE LOGIN WALL ---------------------------------------------------
# Everything is private unless whitelisted. The whitelist covers:
#   * auth endpoints, since you cannot log in without them
#   * health and root, for uptime probes
#   * the signed stream proxies and player, loaded directly by <iframe>, <video>
#     and hls.js, none of which can attach an Authorization header. They are
#     HMAC-signed and a working URL only comes from an authenticated /watch call,
#     so they are gated indirectly
#   * the Ko-fi webhook, called by Ko-fi rather than a browser
#   * docs
#   * /metrics, listed only so a scrape carrying METRICS_TOKEN reaches the
#     handler. The route is not public: it enforces its own token-or-admin check
#     and denies by default when no token is configured.
#
# Defined before the CORS middleware so CORS stays the outermost layer and its
# headers reach even the 401 returned here, which browsers need in order to
# surface the error instead of an opaque CORS failure.
_PUBLIC_EXACT = {
    "/", "/lumi", "/health", "/config", "/openapi.json", "/docs", "/redoc", "/metrics",
}
_PUBLIC_PREFIXES = (
    "/auth/",
    "/kofi/webhook",
    "/changelog",
    "/player",
    # The only stream proxies the backend still serves. An overlay's goes into
    # _DYNAMIC_PUBLIC_PREFIXES instead, so this list names no overlay source.
    "/jellyfin_proxy",
    "/cache_proxy",
    # A <video> and hls.js load these cross-origin and cannot attach the bearer,
    # exactly like /cache_proxy, so they must be public. Each maps its path token
    # back to a file only inside a currently enabled root, re-checked per request,
    # so being public does not widen what they reach.
    "/local_proxy",
    "/local_hls",
    # Loads cross-origin in an <img> with no auth header, so it is signed instead.
    "/local_art",
    # The <track> loads cross-origin with no auth header, so it is signed instead.
    "/subtitles_proxy",
    # Same reasoning as /subtitles_proxy. Dormant unless an operator build injects
    # a manga provider; the public build resolves pages client-side.
    "/manga_proxy",
    # hls.js loads these cross-origin and cannot carry the bearer, so they are
    # signed instead and the fetch runs through the SSRF-guarded client.
    "/iptv_proxy",
    "/docs",
)

# Contributed at import time by the optional overlay, and empty in a base build.
# Its proxies are loaded cross-origin with no auth header and signed instead, so
# they bypass the wall exactly as the operator proxies above do. Kept separate and
# derived from module names, so this file names no overlay source.
_DYNAMIC_PUBLIC_PREFIXES: tuple = ()

# Keeps the login wall from adding a DB round-trip to every content request. The
# common case, a hit, skips the DB entirely, and entries are short-lived so a
# logout takes effect within the TTL. Keyed by the token's SHA-256, never the raw
# token.
_SESSION_OK_TTL = 60.0
_SESSION_OK_MAX = 20_000        # bounds memory
_session_ok_cache: Dict[str, float] = {}


async def _session_is_valid(raw_token: str) -> bool:
    if not raw_token:
        return False
    key = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    exp = _session_ok_cache.get(key)
    if exp is not None and exp > now:
        return True
    # A miss verifies against the DB, off the event loop.
    user = await run_in_threadpool(account_store.get_user_by_session, raw_token)
    if user:
        if len(_session_ok_cache) >= _SESSION_OK_MAX:
            _session_ok_cache.clear()  # cheap bounded reset under abuse
        _session_ok_cache[key] = now + _SESSION_OK_TTL
        return True
    _session_ok_cache.pop(key, None)
    return False


# The same cache for bridge API keys. A miss both validates and touches
# last_used_at, so that write happens at most once per key per TTL rather than on
# every /mw request.
_APIKEY_OK_TTL = 60.0
_APIKEY_OK_MAX = 5_000          # bounds memory
_apikey_ok_cache: Dict[str, float] = {}


async def _apikey_is_valid(raw_key: str) -> bool:
    if not raw_key:
        return False
    key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    now = time.monotonic()
    exp = _apikey_ok_cache.get(key)
    if exp is not None and exp > now:
        return True
    ok = await run_in_threadpool(apikey_store.validate_and_touch, raw_key)
    if ok:
        if len(_apikey_ok_cache) >= _APIKEY_OK_MAX:
            _apikey_ok_cache.clear()  # cheap bounded reset under abuse
        _apikey_ok_cache[key] = now + _APIKEY_OK_TTL
        return True
    _apikey_ok_cache.pop(key, None)
    return False


class LoginWallMiddleware:
    """Pure-ASGI login wall.

    At the ASGI layer rather than BaseHTTPMiddleware, so it adds no buffering to
    the progressive /watch stream: it inspects the request scope, then either
    short-circuits with a 401 or passes the channels straight through."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not Config.REQUIRE_LOGIN:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if (
            scope.get("method") == "OPTIONS"
            or path in _PUBLIC_EXACT
            or path.startswith(_PUBLIC_PREFIXES)
            or (_DYNAMIC_PUBLIC_PREFIXES and path.startswith(_DYNAMIC_PUBLIC_PREFIXES))
        ):
            return await self.app(scope, receive, send)

        token = ""
        api_key = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                val = value.decode("latin-1")
                if val[:7].lower() == "bearer ":
                    token = val.split(" ", 1)[1].strip()
            elif name == b"x-api-key":
                api_key = value.decode("latin-1").strip()

        # A signed-in session is accepted on every gated path.
        if token and await _session_is_valid(token):
            return await self.app(scope, receive, send)

        # Keys are scoped to the bridge alone: a valid X-API-Key unlocks /mw* and
        # nothing else. That is what lets an admin hand one to the movie-web fork
        # without it becoming a skeleton key for the whole backend.
        if (
            (path == "/mw" or path.startswith("/mw/"))
            and api_key
            and await _apikey_is_valid(api_key)
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


# Before CORS, so CORS stays outermost and its headers reach even the 401 this
# returns, which the browser needs in order to surface the error.
app.add_middleware(LoginWallMiddleware)

# --- CORS -------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LumiHeaderMiddleware:
    """Stamp every response with Lumi's voice.

    Pure-ASGI like the login wall, so it touches only the response start message
    and never buffers the body. ``X-Lumi`` carries a rotating ASCII quip and
    ``X-Powered-By`` names the empress. A quip that fails to encode is dropped
    rather than breaking the response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                try:
                    headers.append((b"x-lumi", lumi.header_quip().encode("latin-1")))
                except Exception:
                    pass
                headers.append(
                    (b"x-powered-by", f"{lumi.EMPRESS}, {lumi.TITLE}".encode("latin-1"))
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


# After CORS, so CORS stays outermost. Appends headers only and never buffers.
app.add_middleware(LumiHeaderMiddleware)


class RequestContextMiddleware:
    """Mint a request id, bind it for logging, and record the HTTP metrics.

    Pure-ASGI for the same reason as the two above: BaseHTTPMiddleware wraps the
    response in an anyio stream, which would buffer the /watch body and stall
    playback until the slowest scraper finished.

    Three things happen here:

    * ``X-Request-ID`` is taken from the request, since a reverse proxy may
      already have set one, or minted, then bound to a ContextVar so every log
      line from this request carries it. It is echoed back so a user can quote it.
    * Latency is measured to the response headers, not the last body byte, so
      /watch's multi-second stream does not swamp the histogram. The streaming
      side has its own crimson_watch_* metrics.
    * The route template is the metric label, never the raw path, which would mint
      one timeseries per episode.

    Added last, so it is outermost and a request the login wall rejects still gets
    an id and is still counted. The wall short-circuits before routing, so its
    401s land in the ``__unmatched__`` bucket rather than their real route. That is
    the desirable direction: unauthenticated traffic cannot mint label values.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_id = ""
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                request_id = observability.clean_request_id(value.decode("latin-1"))
                break
        if not request_id:
            request_id = observability.new_request_id()

        scope.setdefault("state", {})["request_id"] = request_id
        token = observability.set_request_id(request_id)

        method = observability.method_label(scope.get("method"))
        started = time.monotonic()
        observability.track_in_progress(method, 1)
        recorded = False

        async def send_wrapper(message):
            nonlocal recorded
            if message["type"] == "http.response.start" and not recorded:
                recorded = True
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                # Set by the router during dispatch, so it is available by the
                # time the response flows back through here. A 404 has no route
                # and collapses to a single bucket.
                observability.record_http_request(
                    method,
                    observability.route_label(scope),
                    message.get("status", 0),
                    time.monotonic() - started,
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if not recorded:
                # Nothing ever started: the client hung up, or the app raised
                # before sending. Recorded as 499, nginx's client-closed-request,
                # so the gauge below cannot drift upward forever.
                observability.record_http_request(
                    method, observability.route_label(scope), 499,
                    time.monotonic() - started,
                )
            observability.track_in_progress(method, -1)
            observability.reset_request_id(token)


app.add_middleware(RequestContextMiddleware)

# --- ROUTERS ----------------------------------------------------------------
# Engine routers first, since their prefixes are all distinct, then the core
# routers from web.routes.

# Sign-in, favorites and watch progress.
app.include_router(account_router)

# Gated by require_admin on every route; the login wall already covers /admin.
app.include_router(admin_router)

# Ko-fi webhook ingest and the public supporters list.
app.include_router(supporters_router)

# A cached, public view of this repo's GitHub Releases.
app.include_router(changelog_router)

# Genre-based recommendations from the viewer's favorites and watch history.
app.include_router(recommend_router)

# Gated three ways and deny-by-default, so mounting it exposes nothing until an
# admin both switches it on and grants an account access. Its tools are thin
# wrappers over the engines above.
app.include_router(chat_router)

# External subtitle tracks. /subtitles is authed, while /subtitles_proxy is public
# and signed because a <track> cannot carry auth.
app.include_router(subtitles_router)

# Intro and outro skip timestamps. Authed, anime-only and best-effort.
app.include_router(skiptimes_router)

# The reading surface. Chapters and pages resolve in the viewer's browser, or via
# an injected provider on an operator build. /manga_proxy is public and signed but
# dormant without a provider; the rest sits behind the login wall.
app.include_router(manga_router)

# A read-only catalogue of free-to-air broadcasts indexed by iptv-org. Browse and
# detail sit behind the login wall; /iptv_proxy is public and signed.
app.include_router(iptv_router)

# The core surface: system, discovery, watch, metadata and proxies.
for _router in all_routers:
    app.include_router(_router)

# --- ENGINE HANDLER INJECTION ----------------------------------------------
# Several engine routers call back into logic in the web layer, injected here so
# those engines import neither the pipeline nor this module:
#
#   * the account router enriches progress rows with next-episode hints and fires
#     the continue-watching warmup
#   * the admin router runs the forced resync, the system snapshot and the
#     source-health sweep
set_episode_enricher(_enrich_progress_rows)
set_warmup_handler(schedule_warmup)
set_resync_handler(forced_resync)
set_system_handler(admin_system_info)
set_source_health_handler(admin_source_health)


# --- OPTIONAL BUILD-TIME OVERLAY STREAM PROXIES -----------------------------
# Same-origin relays for any overlaid module shipping a ``proxy_fetch``. Each is
# schema-hidden, and the HMAC verification and host allow-list live inside that
# module. A base build has none. Routes are derived from the module names and the
# wiring from each fetch signature, so this file names no overlaid source.
def _register_overlay_stream_proxies():
    global _DYNAMIC_PUBLIC_PREFIXES

    import resolvers as _res_pkg

    already_wired = {"jellyfin", "local", "cache"}

    def _signed_stream(fetch_fn):
        async def _route(request: Request):
            try:
                result = await fetch_fn(
                    url=request.query_params.get("u"),
                    sig=request.query_params.get("s"),
                    range_header=request.headers.get("range"),
                )
            except ValueError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except httpx.RequestError as e:
                logger.error(f"overlay proxy upstream error: {e}")
                raise HTTPException(status_code=502, detail="Upstream fetch failed")
            return _proxy_response(*result)
        return _route

    def _signed_stream_with_headers(fetch_fn):
        async def _route(request: Request):
            try:
                result = await fetch_fn(
                    url=request.query_params.get("u"),
                    origin=request.query_params.get("o"),
                    referer=request.query_params.get("r"),
                    sig=request.query_params.get("s"),
                    range_header=request.headers.get("range"),
                )
            except ValueError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except httpx.RequestError as e:
                logger.error(f"overlay proxy upstream error: {e}")
                raise HTTPException(status_code=502, detail="Upstream fetch failed")
            return _proxy_response(*result)
        return _route

    def _reverse_proxy(fetch_fn):
        async def _route(request: Request, host: str, path: str):
            body = await request.body() if request.method == "POST" else None
            try:
                result = await fetch_fn(
                    host=host,
                    path=path,
                    query_string=request.url.query,
                    method=request.method,
                    body=body,
                    range_header=request.headers.get("range"),
                )
            except ValueError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except httpx.RequestError as e:
                logger.error(f"overlay proxy upstream error: {e}")
                raise HTTPException(status_code=502, detail="Upstream fetch failed")
            return _proxy_response(*result)
        return _route

    public_prefixes = []
    for info in pkgutil.iter_modules(_res_pkg.__path__):
        name = info.name
        if name in already_wired or name.startswith("_") or "test" in name:
            continue
        try:
            module = importlib.import_module(f"resolvers.{name}")
        except Exception:
            continue
        fetch_fn = getattr(module, "proxy_fetch", None)
        if fetch_fn is None:
            continue
        params = set(inspect.signature(fetch_fn).parameters)
        if {"host", "path"} <= params:
            route, suffix = _reverse_proxy(fetch_fn), "/h/{host}/{path:path}"
            methods = ["GET", "POST"]
        elif {"origin", "referer"} <= params:
            route, suffix, methods = _signed_stream_with_headers(fetch_fn), "", ["GET"]
        elif {"url", "sig"} <= params:
            route, suffix, methods = _signed_stream(fetch_fn), "", ["GET"]
        else:
            continue
        app.add_api_route(
            f"/{name}_proxy{suffix}", route, methods=methods,
            name=f"{name}_proxy", include_in_schema=False,
        )
        public_prefixes.append(f"/{name}_proxy")

    if public_prefixes:
        _DYNAMIC_PUBLIC_PREFIXES = tuple(public_prefixes)
        logger.info("registered %d overlay stream prox(y/ies)", len(public_prefixes))


_register_overlay_stream_proxies()


# --- ERROR HANDLERS ---------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Keeps the real technical detail in ``error``, which the frontend may key on,
    and adds Lumi's voiced ``message`` for the banner."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "message": lumi.voiced_error(exc.status_code),
            "status_code": exc.status_code
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Last-resort handler: logs the traceback and returns a voiced 500."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "message": lumi.voiced_error(500),
            "detail": str(exc) if os.getenv("DEBUG") else None
        }
    )
