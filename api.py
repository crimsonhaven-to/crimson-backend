"""The Crimson backend's FastAPI assembler — the "brain, not pipe" entrypoint.

This module used to be one ~3,500-line file holding the app, every route, the
scrape/resolve pipeline, the DB helpers and the injected engine handlers. It is now
the thin *assembler*: it creates the app, mounts the middleware (login wall + CORS +
Lumi header), owns the lifespan (schedulers, DB init, warm caches), registers the
optional build-time overlay's stream proxies, wires the exception handlers, and
includes the routers from ``web.routes`` — the actual endpoints and logic live in
the ``web`` package now (see ``web/__init__.py`` for the map).
"""

import asyncio
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
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from starlette.concurrency import run_in_threadpool

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.rate_limit import limiter
from core import lumi
from core import config_report
from core.version import VERSION
from core.config import Config
from core.db_pool import close_pool
from core.http_client import (
    open_client as open_http_client,
    close_client as close_http_client,
)
from core.response_cache import purge_expired_cache
from cache_engine.downloader import manager as cache_manager
from resolvers import _crimson_proxy

from account_engine import router as account_router, store as account_store
from account_engine.routes import set_episode_enricher, set_warmup_handler
from account_engine.admin_routes import (
    router as admin_router,
    set_resync_handler,
    set_system_handler,
    set_source_health_handler,
)
from apikey_engine import store as apikey_store
from supporters_engine import router as supporters_router, store as supporters_store
from changelog_engine import router as changelog_router, service as changelog_service
from recommend_engine import router as recommend_router
from subtitles_engine import router as subtitles_router, service as subtitles_service
from skiptimes_engine import router as skiptimes_router
from metadata_engine import maintenance as metadata_maintenance

# The HTTP layer — singletons, the injected engine handlers, and the routers. The
# routes and their logic all live under the ``web`` package now; this file only
# assembles them. (See web/__init__.py for the full map.)
from web.context import (
    db_engine,
    local_source_store,
    cache_store,
    telemetry_store,
)
from web.pipeline import _enrich_progress_rows
from web.warmup import schedule_warmup
from web.admin_handlers import admin_source_health, admin_system_info, forced_resync
from web.routes import all_routers
from web.routes.proxies import _proxy_response

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Load environment variables (defensive; core.config already loads its own).
load_dotenv()


# --- LIFESPAN MANAGEMENT ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    # Startup
    logger.info("Starting up FastAPI application...")

    # Log which optional, env-gated features are on/off (presence only, no secret
    # values) so a "dark" source is diagnosable at a glance from the boot log.
    config_report.log_report(logger)

    # Open the shared HTTP client (kept warm for the whole process lifetime).
    open_http_client()

    # Initialize databases (idempotent — safe on every replica).
    db_engine.init_db()
    account_store.init_db()  # account tables (untouched by mapping resyncs)
    apikey_store.init_db()  # movie-web bridge API keys (resync-safe)
    supporters_store.init_db()  # Ko-fi supporters ledger (also resync-safe)
    local_source_store.init_db()  # admin-managed local media sources (resync-safe)
    cache_store.init_db()  # server-side video cache tables (resync-safe)
    telemetry_store.init_db()  # anonymous resolve telemetry (resync-safe)

    # Seed admin accounts from ADMIN_EMAILS (idempotent; only promotes accounts
    # that already exist). Lets the operator reach the /admin dashboard without
    # editing the DB by hand. Safe on every replica.
    if Config.ADMIN_EMAILS:
        try:
            promoted = account_store.bootstrap_admins(Config.ADMIN_EMAILS)
            if promoted:
                logger.info(f"Promoted {promoted} account(s) to admin from ADMIN_EMAILS")
        except Exception as e:
            logger.error(f"Admin bootstrap failed: {e}")

    # One scheduler per replica. It always owns cheap housekeeping (expired
    # session/challenge purge); the heavy Fribb mapping resync is added to it on
    # exactly ONE replica (RUN_DB_SYNC).
    scheduler = BackgroundScheduler()

    # Housekeeping (every replica): consume_challenge / get_user_by_session already
    # delete rows on access, but abandoned challenges (requested, never completed)
    # would otherwise pile up until the next restart — sweep them periodically.
    def _purge_expired():
        try:
            account_store.purge_expired()
        except Exception as e:
            logger.error(f"Expired session/challenge purge failed: {e}")
        # Also sweep expired api_cache rows. consume-on-read never deletes them,
        # and every unique search query writes a row, so the table would grow
        # unbounded otherwise.
        try:
            n = purge_expired_cache()
            if n:
                logger.info(f"Purged {n} expired api_cache rows")
        except Exception as e:
            logger.error(f"Expired api_cache purge failed: {e}")

    scheduler.add_job(
        _purge_expired,
        trigger=IntervalTrigger(hours=6),
        id="purge_expired_job",
        replace_existing=True,
    )

    # Changelog cache (every replica keeps its own in-process copy; ETag
    # conditional requests keep the refresh near-free against GitHub's rate
    # limit). Only active when a GITHUB_TOKEN is configured. The initial warm-up
    # runs off the event loop so a slow/unreachable GitHub never delays startup;
    # the periodic refresh runs in the scheduler's worker thread.
    if changelog_service.configured():
        async def _warm_changelog():
            try:
                await run_in_threadpool(changelog_service.refresh)
                logger.info("Changelog cache warmed from GitHub Releases")
            except Exception as e:
                logger.error(f"Initial changelog warm-up failed (will retry on schedule): {e}")

        asyncio.create_task(_warm_changelog())  # fire-and-forget

        def _refresh_changelog():
            try:
                changelog_service.refresh()
            except Exception as e:
                logger.error(f"Changelog refresh failed: {e}")

        scheduler.add_job(
            _refresh_changelog,
            trigger=IntervalTrigger(minutes=30),
            id="changelog_refresh_job",
            replace_existing=True,
        )
    else:
        logger.info("GITHUB_TOKEN not set — /changelog will return 503 until configured")

    # CORS-proxy health cache (every replica keeps its own, since each routes
    # independently). Periodically probes every CRIMSON_PROXY_BASE host so proxy_url
    # routes only to the ones that are up — automatic failover between the Cloudflare
    # and Netlify deploys. Cheap (1–2 GETs per host); only runs when configured.
    if _crimson_proxy.is_enabled():
        async def _warm_proxy_health():
            try:
                await _crimson_proxy.refresh_health()
                logger.info("CORS proxy health cache warmed")
            except Exception as e:
                logger.error(f"Initial proxy health probe failed (will retry on schedule): {e}")

        asyncio.create_task(_warm_proxy_health())  # fire-and-forget; don't delay startup

        def _refresh_proxy_health():
            try:
                asyncio.run(_crimson_proxy.refresh_health())
            except Exception as e:
                logger.error(f"Proxy health refresh failed: {e}")

        scheduler.add_job(
            _refresh_proxy_health,
            trigger=IntervalTrigger(minutes=2),
            id="proxy_health_job",
            replace_existing=True,
        )
    else:
        logger.info("CRIMSON_PROXY_BASE not set — external CORS proxy disabled, /sign returns 503")

    if subtitles_service.configured():
        logger.info("OpenSubtitles configured — /subtitles is enabled")
    else:
        logger.info("OPENSUBTITLES_API_KEY not set — /subtitles will return 503 until configured")

    # DEMO_MODE nightly reset (demo.crimsonhaven.to). Signup is open (invite gate
    # bypassed in account_engine.routes), so all non-admin account data is wiped each
    # night to bound growth. Pinned to the single RUN_DB_SYNC replica so multiple
    # replicas don't race the same DELETE; a demo normally runs one replica with
    # RUN_DB_SYNC=true (the compose default).
    if Config.DEMO_MODE:
        logger.warning(
            "DEMO_MODE is ON — signup invite gate is bypassed; non-admin data resets "
            f"nightly at {Config.DEMO_RESET_HOUR:02d}:00 (server time)"
        )
        if Config.RUN_DB_SYNC:
            def _demo_reset():
                try:
                    res = account_store.wipe_demo_data()
                    logger.info(f"DEMO_MODE nightly reset done: {res}")
                except Exception as e:
                    logger.error(f"DEMO_MODE nightly reset failed: {e}")

            scheduler.add_job(
                _demo_reset,
                trigger=CronTrigger(hour=Config.DEMO_RESET_HOUR, minute=0),
                id="demo_reset_job",
                replace_existing=True,
            )
        else:
            logger.info("DEMO_MODE: this replica is not RUN_DB_SYNC — the nightly reset runs on the sync replica")

    # The Fribb resync rebuilds the mapping tables wholesale. In a multi-replica
    # Swarm deploy only ONE replica should own it (RUN_DB_SYNC), otherwise every
    # replica downloads + rebuilds in lockstep, wasting bandwidth and contending
    # on the shared DB. Other replicas just serve from the synced DB.
    if not Config.RUN_DB_SYNC:
        logger.info("RUN_DB_SYNC is disabled — this replica will not run the mapping resync")
    else:
        # Run initial sync
        try:
            await db_engine.sync_database_async()
            logger.info("Initial database sync completed")
        except Exception as e:
            logger.error(f"Initial database sync failed: {e}")

        # Periodic sync. BackgroundScheduler runs jobs in a worker thread with no
        # running event loop, so the job spins up its own.
        def _scheduled_sync():
            try:
                asyncio.run(db_engine.sync_database_async())
            except Exception as e:
                logger.error(f"Scheduled sync failed: {e}")

        scheduler.add_job(
            _scheduled_sync,
            trigger=IntervalTrigger(hours=24),
            id="db_sync_job",
            replace_existing=True,
        )

    # Non-anime metadata maintenance (tmdb_shows / tmdb_movies). ALL of it is pinned
    # to the single RUN_DB_SYNC replica (api-sync), so exactly one container ever
    # churns this much metadata. Three pieces:
    #   1. a nightly slice refresh (no upstream tells us when TMDB changed, so the
    #      catalogue is swept oldest-1/N each night, cycling over METADATA_REFRESH_BUCKETS);
    #   2. a short-interval drainer for backfill jobs the Admin dashboard queues
    #      (the button hits a portless-api-sync-unreachable serving replica, so the
    #      request arrives via the metadata_backfill_jobs table);
    #   3. an optional one-shot backfill at startup (RUN_METADATA_BACKFILL).
    if Config.RUN_DB_SYNC:
        def _nightly_metadata_refresh():
            try:
                shows, movies = asyncio.run(metadata_maintenance.refresh_daily_slice())
                if shows or movies:
                    logger.info(f"Nightly metadata refresh: {shows} show(s), {movies} movie(s)")
            except Exception as e:
                logger.error(f"Nightly metadata refresh failed: {e}")

        scheduler.add_job(
            _nightly_metadata_refresh,
            trigger=CronTrigger(hour=Config.METADATA_REFRESH_HOUR, minute=0),
            id="metadata_nightly_refresh_job",
            replace_existing=True,
        )

        def _drain_backfill_queue():
            try:
                asyncio.run(metadata_maintenance.run_pending_backfill())
            except Exception as e:
                logger.error(f"Backfill drain failed: {e}")

        # Poll the queue often so an admin-triggered backfill starts promptly. A run
        # can take minutes; APScheduler's default max_instances=1 skips overlapping
        # ticks, so a long backfill won't stack.
        scheduler.add_job(
            _drain_backfill_queue,
            trigger=IntervalTrigger(minutes=1),
            id="metadata_backfill_drain_job",
            replace_existing=True,
        )

        if Config.RUN_METADATA_BACKFILL:
            async def _run_backfill():
                try:
                    shows, movies = await metadata_maintenance.backfill_catalogue()
                    logger.info(f"Startup metadata backfill seeded {shows} show(s), {movies} movie(s)")
                except Exception as e:
                    logger.error(f"Startup metadata backfill failed: {e}")

            asyncio.create_task(_run_backfill())  # fire-and-forget; paced internally

    scheduler.start()
    logger.info("Background scheduler started")
    app.state.scheduler = scheduler

    # Server-side video-cache download worker (background ffmpeg). Only the
    # dedicated cache-worker service runs it (RUN_CACHE_WORKER); api/api-sync just
    # mint tickets + claim pending rows. The job lives in Postgres (claim_download),
    # so a download survives an api redeploy and any worker can drain the queue.
    if Config.RUN_CACHE_WORKER:
        await cache_manager.start_worker()
    else:
        logger.info(
            "RUN_CACHE_WORKER disabled — this replica mints/claims cache rows but "
            "does not download (the cache-worker service does)"
        )

    yield

    # Shutdown
    logger.info("Shutting down...")
    await cache_manager.stop()
    if getattr(app.state, 'scheduler', None) is not None:
        app.state.scheduler.shutdown()
    await close_http_client()
    close_pool()  # drain the PostgreSQL connection pool
    logger.info("Shutdown complete")


# Create FastAPI app with lifespan
app = FastAPI(
    title="Anime Streaming API",
    description="API for streaming anime with multi-season support",
    version=VERSION,
    lifespan=lifespan,
    # orjson encodes every plain `return {...}` endpoint several times faster than
    # stdlib json. The hand-rolled streaming (NDJSON /watch) and gzip (/catalogue)
    # responses build their own Response objects and are unaffected by this.
    default_response_class=ORJSONResponse,
)

# Rate limiting (slowapi). Registered on app.state so the @limiter.limit
# decorators on the expensive/abusable endpoints take effect; the 429 handler
# returns a clean JSON error with Retry-After.
app.state.limiter = limiter


async def _voiced_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Like slowapi's default 429, but in Lumi's voice. Delegates to the original
    to get the correct status + ``Retry-After``, then re-skins the body."""
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
# Everything is private unless explicitly whitelisted. The whitelist covers:
#   * auth endpoints (you can't log in without them),
#   * health/root (uptime probes),
#   * the signed stream proxies + player — these are loaded directly by <iframe>/
#     <video>/hls.js which can't attach an Authorization header; they're already
#     HMAC-signed and you only get a working URL from an authenticated /watch call,
#     so they're gated indirectly,
#   * the Ko-fi webhook (called by Ko-fi, not a browser),
#   * docs.
# Defined BEFORE the CORS middleware below so CORS remains the outermost layer and
# its headers are attached even to the 401 we return here (browsers need that to
# surface the error instead of an opaque CORS failure).
_PUBLIC_EXACT = {"/", "/lumi", "/health", "/config", "/openapi.json", "/docs", "/redoc"}
_PUBLIC_PREFIXES = (
    "/auth/",
    "/kofi/webhook",
    "/changelog",
    "/player",
    # Operator-owned source proxies (the only stream proxies the backend still
    # serves). Third-party source proxies were removed with their scrapers.
    "/jellyfin_proxy",
    "/cache_proxy",
    "/febbox_proxy",
    # The subtitle <track> loads cross-origin with no auth header (signed instead).
    "/subtitles_proxy",
    "/docs",
)

# Extra public path prefixes contributed at import time by the optional build-time
# source overlay (empty in a base build). Their stream proxies are loaded cross-origin
# by the player with no auth header (HMAC-signed instead), so they bypass the login
# wall the same way the operator proxies above do. Kept separate + derived from the
# overlaid module names so this committed file names no overlay source.
_DYNAMIC_PUBLIC_PREFIXES: tuple = ()

# Tiny in-process cache of validated session tokens so the login wall doesn't add
# a DB round-trip to every content request. A hit (the common case) skips the DB
# entirely; entries are short-lived so a logout/expiry takes effect within the
# TTL. Keyed by the token's SHA-256 (never the raw token).
_SESSION_OK_TTL = 60.0          # seconds
_SESSION_OK_MAX = 20_000        # hard cap to bound memory
_session_ok_cache: Dict[str, float] = {}


async def _session_is_valid(raw_token: str) -> bool:
    if not raw_token:
        return False
    key = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    exp = _session_ok_cache.get(key)
    if exp is not None and exp > now:
        return True
    # Cache miss — verify against the DB off the event loop.
    user = await run_in_threadpool(account_store.get_user_by_session, raw_token)
    if user:
        if len(_session_ok_cache) >= _SESSION_OK_MAX:
            _session_ok_cache.clear()  # cheap, bounded reset under abuse
        _session_ok_cache[key] = now + _SESSION_OK_TTL
        return True
    _session_ok_cache.pop(key, None)
    return False


# Same short-lived validity cache for movie-web bridge API keys (see apikey_engine).
# Keyed by SHA-256 of the raw key; a hit skips the DB on the hot path. A cache miss
# validates AND touches last_used_at, so that write happens at most once per key per
# TTL rather than on every /mw request.
_APIKEY_OK_TTL = 60.0           # seconds
_APIKEY_OK_MAX = 5_000          # hard cap to bound memory
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
            _apikey_ok_cache.clear()  # cheap, bounded reset under abuse
        _apikey_ok_cache[key] = now + _APIKEY_OK_TTL
        return True
    _apikey_ok_cache.pop(key, None)
    return False


class LoginWallMiddleware:
    """Pure-ASGI login wall. Implemented at the ASGI layer (not BaseHTTPMiddleware)
    so it adds zero buffering to the progressive NDJSON /watch stream — it only
    inspects the request scope, then either short-circuits with a 401 or passes
    the untouched send/receive channels straight through."""

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

        # A normal signed-in session is accepted on every gated path.
        if token and await _session_is_valid(token):
            return await self.app(scope, receive, send)

        # API keys are deliberately scoped to the movie-web bridge ONLY: a valid
        # X-API-Key unlocks /mw* and nothing else (not /account, /admin, or the
        # catalogue). That scoping is what lets an admin hand a key to the
        # movie-web fork without it becoming a skeleton key for the whole backend.
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


# Added BEFORE CORS so CORS stays the outermost layer and its headers are applied
# even to the 401 this returns (the browser needs them to surface the error).
app.add_middleware(LoginWallMiddleware)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LumiHeaderMiddleware:
    """Stamp every response with Lumi's voice. Pure-ASGI (like the login wall) so
    it only touches the response *start* message — it appends two headers and
    never buffers the body, leaving the progressive NDJSON /watch stream untouched.

    ``X-Lumi`` carries a rotating, ASCII-only sarcastic quip (devtools easter egg);
    ``X-Powered-By`` names the empress. Best-effort: a quip that somehow fails to
    encode is simply dropped rather than breaking the response."""

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


# Added after CORS so CORS stays outermost; this only appends response headers and
# never buffers, so the NDJSON stream is unaffected.
app.add_middleware(LumiHeaderMiddleware)

# --- ROUTERS ----------------------------------------------------------------
# Engine routers first (their prefixes — /account, /admin, /supporters, /changelog,
# /recommendations, /subtitles, /skiptimes — are all distinct), then the core
# routers from web.routes. Order is preserved from the old single-file api.py.

# Account system (mnemonic/Ed25519 sign-in, favorites, watch progress).
app.include_router(account_router)

# Admin dashboard (user management, invite minting, metadata resync, stats).
# Gated by require_admin on every route; the login wall already covers /admin.
app.include_router(admin_router)

# Ko-fi supporters (webhook ingest + public "Lumi's Loved Mortals" list).
app.include_router(supporters_router)

# Public changelog (cached view of this repo's GitHub Releases).
app.include_router(changelog_router)

# "What to watch next" — genre-based recommendations derived from the viewer's
# favorites + watch history (read-only, additive; see recommend_engine).
app.include_router(recommend_router)

# OpenSubtitles-backed external subtitle tracks for the player. /subtitles is
# authed (search, no quota spent); /subtitles_proxy is public + signed (the
# <track> can't carry auth) — see subtitles_engine + the _PUBLIC_PREFIXES entry.
app.include_router(subtitles_router)

# AniSkip-backed intro/outro skip timestamps for the anime player. /skiptimes is
# authed (behind the login wall); anime-only (resolves anilist_id -> mal_id) and
# best-effort — see skiptimes_engine.
app.include_router(skiptimes_router)

# The core surface (system, discovery, watch, metadata, proxies) — see web.routes.
for _router in all_routers:
    app.include_router(_router)

# --- ENGINE HANDLER INJECTION ----------------------------------------------
# Several engine routers call back into logic that lives in the web layer (the heavy
# TMDB/pipeline helpers), injected here so those engines don't import the pipeline
# (or this module). Same dependency-injection pattern throughout.
#
#   * the account router enriches watch-progress rows with next-episode hints and
#     fires the continue-watching warmup;
#   * the admin router runs the forced Fribb resync, the system snapshot, and the
#     source-health probe sweep.
set_episode_enricher(_enrich_progress_rows)
set_warmup_handler(schedule_warmup)
set_resync_handler(forced_resync)
set_system_handler(admin_system_info)
set_source_health_handler(admin_source_health)


# --- OPTIONAL BUILD-TIME OVERLAY STREAM PROXIES -----------------------------
# Optional same-origin stream relays for any overlaid module that ships a
# ``proxy_fetch`` (present only when the build-time overlay added it). Each mirrors
# /febbox_proxy: schema-hidden, with the HMAC verification / host allow-list living
# inside the module's own ``proxy_fetch``. A base build has none, so nothing is added.
# Routes are derived from the module names + the wiring shape from each fetch
# signature, so this file names no overlaid source.
def _register_overlay_stream_proxies():
    global _DYNAMIC_PUBLIC_PREFIXES

    import resolvers as _res_pkg

    already_wired = {"jellyfin", "febbox", "local", "cache"}

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


# --- ERROR HANDLERS ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler. Keeps the real technical detail in ``error``
    (the frontend may key on it) and adds Lumi's voiced ``message`` for the banner."""
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
    """Global exception handler"""
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
