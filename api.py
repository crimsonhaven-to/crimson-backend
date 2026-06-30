import asyncio
import os
import gzip
import hashlib
import platform
import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from contextlib import asynccontextmanager

import httpx
import json
import orjson
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, ORJSONResponse, Response, StreamingResponse
from fastapi.requests import Request
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# Import the scraper/resolver registries + metadata engine.
#
# The public backend no longer scrapes third-party sites (that moved to the
# private ``crimson-sources`` package, running client/extension/proxy-side — see
# New_System.md). What's left are the operator-owned sources: the server cache,
# local NAS dirs, and the operator's own Jellyfin server, plus an inert template.
from scrapers import ALL_SCRAPERS
from resolvers import ALL_RESOLVERS
from resolvers.jellyfin import (
    proxy_fetch as jellyfin_proxy_fetch,
    is_configured as jellyfin_is_configured,
    JellyfinResolver,
)
from scrapers.jellyfin_scraper import JellyfinScraper
# Febbox is wired ONLY into the secret-bound `/resolve` grant (not ALL_SCRAPERS /
# ALL_RESOLVERS, so it never appears in the public `/watch` pipeline). It self-
# disables unless FEBBOX_UI_TOKEN is set, so a stock public deployment runs nothing
# here; an operator who has the token gets the resolve grant the client engine uses.
from resolvers.febbox import (
    proxy_fetch as febbox_proxy_fetch,
    FebboxResolver,
    is_configured as febbox_is_configured,
)
from scrapers.showbox_scraper import ShowBoxScraper
from resolvers import _crimson_proxy
from local_engine.db import LocalSourceStore
from local_engine.fs import (
    safe_resolve as local_safe_resolve,
    safe_resolve_transcode as local_safe_resolve_transcode,
    media_type_for as local_media_type,
    is_configured as local_is_configured,
)
from local_engine import transcode as local_transcode
from cache_engine.db import CacheStore
from cache_engine.fs import (
    safe_resolve as cache_safe_resolve,
    media_type_for as cache_media_type,
)
from cache_engine.downloader import manager as cache_manager, ffmpeg_available
from telemetry_engine import TelemetryStore
from core.player import render_player, is_safe_src
from metadata_engine.db_handler import MappingDatabaseEngine
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
from core.db_pool import get_pool, close_pool, pool_stats
from core import lumi
from core import source_health
from core import config_report
from core.contracts import (
    build_done_line,
    build_meta_line,
    build_stream_line,
    build_unaired_line,
)
from core.rate_limit import limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.concurrency import run_in_threadpool
from core.config import Config
from core.http_client import (
    http_client,
    open_client as open_http_client,
    close_client as close_http_client,
)
from core.response_cache import (
    _local_cache,
    _local_get,
    _local_set,
    get_cached_response,
    set_cached_response,
    purge_expired_cache,
)
from metadata_engine.tmdb import (
    _tmdb_img,
    fetch_tmdb_show,
    fetch_tmdb_movie,
    fetch_tmdb_metadata,
    _season_episode_info,
    fetch_tmdb_search_results,
    fetch_trending_anime,
    fetch_tmdb_show_search_results,
    fetch_trending_shows,
    fetch_tmdb_movie_search_results,
    fetch_trending_movies,
    fetch_tmdb_localized_titles,
    fetch_tmdb_imdb_id,
)
from metadata_engine.anilist import fetch_anilist_metadata, _empty
from metadata_engine import maintenance as metadata_maintenance

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Single source of truth for the API version — fed to both the FastAPI app
# metadata (OpenAPI/docs) and the "/" root greeting.
VERSION = "16.2.0"

# Wall-clock at process start — the admin dashboard derives this replica's uptime
# from it. Module-load time is close enough to "boot" for an operator metric.
_PROCESS_STARTED_AT = time.time()

# Admin-managed local media sources (the "Local" direct-play source). The store
# is schema-init'd in lifespan; the scraper/resolver read the enabled roots
# directly via their own LocalSourceStore (the enabled-roots cache is class-wide).
local_source_store = LocalSourceStore()

# Server-side video cache (downloads played episodes to a NAS target and replays
# them as a named source). Schema-init'd + download manager started in lifespan;
# the scraper/resolver/proxy read enabled targets via their own CacheStore.
cache_store = CacheStore()

# Anonymous per-source resolve telemetry (client beacons -> daily aggregates).
# Restores the source-success visibility lost when resolving moved client-side.
telemetry_store = TelemetryStore()


# Load environment variables
load_dotenv()


# Initialize database engine (storage is the shared PostgreSQL pool; see db_pool)
db_engine = MappingDatabaseEngine(tmdb_api_key=Config.TMDB_API_KEY)


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
    # Signed Febbox subtitle relay (srt->VTT) for the operator-only /resolve grant;
    # loaded cross-origin by the player's <track> with no auth header (HMAC-signed).
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

# Account system (mnemonic/Ed25519 sign-in, favorites, watch progress).
app.include_router(account_router)

# Admin dashboard (user management, invite minting, metadata resync, stats).
# Gated by require_admin on every route; the login wall already covers /admin.
app.include_router(admin_router)


# The admin "trigger metadata resync" endpoint runs the same forced Fribb
# rebuild as metadata_engine.resync, but in-process on the live db_engine (warm
# pool, MVCC-safe single transaction). Injected here so admin_routes doesn't have
# to import the engine (and api.py).
async def _admin_forced_resync():
    await db_engine.sync_database_async(force=True)


set_resync_handler(_admin_forced_resync)

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

# --- DATABASE HELPER FUNCTIONS ---
def get_db_connection():
    """Borrow a pooled PostgreSQL connection as a context manager.

    Returns the pool's connection context manager, so the existing
    ``with get_db_connection() as conn:`` call sites keep working unchanged: the
    transaction commits on a clean exit (rolls back on error) and the connection
    returns to the pool. FastAPI serves these synchronous DB calls from its
    thread pool, and the pool is thread-safe, so many workers (and replicas) can
    share the same external database concurrently.
    """
    return get_pool().connection()

def get_anilist_id(tmdb_id: int, season_number: int) -> Optional[int]:
    """Query mapped AniList ID from TMDB ID and season"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT anilist_id FROM tmdb_seasons WHERE tmdb_id = %s AND season_number = %s",
                (tmdb_id, season_number)
            )
            row = cursor.fetchone()
            return row["anilist_id"] if row else None
    except Exception as e:
        logger.error(f"Database error in get_anilist_id: {e}")
        return None

def get_tmdb_season(anilist_id: int) -> Optional[Tuple[int, Optional[int]]]:
    """
    Reverse lookup: returns (tmdb_id, season_number) for an anilist_id.

    Falls back to tmdb_extras (specials/OVAs/movies), in which case
    season_number is None.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tmdb_id, season_number FROM tmdb_seasons WHERE anilist_id = %s",
                (anilist_id,)
            )
            row = cursor.fetchone()
            if row:
                return (row["tmdb_id"], row["season_number"])

            # Not a numbered season — maybe a special/OVA/movie.
            cursor.execute(
                "SELECT tmdb_id FROM tmdb_extras WHERE anilist_id = %s LIMIT 1",
                (anilist_id,)
            )
            row = cursor.fetchone()
            return (row["tmdb_id"], None) if row else None
    except Exception as e:
        logger.error(f"Database error in get_tmdb_season: {e}")
        return None

def get_anime_genres(anilist_id: int) -> List[str]:
    """Genres for a single anime, read from the local anime_entries DB.

    Same source the catalogue uses (genres is a JSON-encoded list, null for
    entries synced before the column existed). Cheap single-row read so the
    /overview endpoint can ship genres without an extra external API call.
    Returns [] for non-anime / unknown ids.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT genres FROM anime_entries WHERE anilist_id = %s",
                (anilist_id,)
            )
            row = cursor.fetchone()
        if not row or not row["genres"]:
            return []
        return json.loads(row["genres"])
    except (TypeError, ValueError):
        return []
    except Exception as e:
        logger.error(f"Database error in get_anime_genres: {e}")
        return []

def get_show_seasons(tmdb_id: int) -> List[Dict]:
    """Returns all seasons with season_number, anilist_id, title_romaji, etc."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.season_number, s.anilist_id, e.title_romaji, e.title_english, e.anime_type
                FROM tmdb_seasons s
                JOIN anime_entries e ON s.anilist_id = e.anilist_id
                WHERE s.tmdb_id = %s
                ORDER BY s.season_number
            """, (tmdb_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_show_seasons: {e}")
        return []


def get_anime_entry(anilist_id: Optional[int]) -> Dict:
    """Returns the anime_entries row (titles, type, year) for an anilist_id."""
    if not anilist_id:
        return {}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM anime_entries WHERE anilist_id = %s", (anilist_id,))
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Database error in get_anime_entry: {e}")
        return {}

def get_show_extras(tmdb_id: int) -> List[Dict]:
    """Returns specials/OVAs/movies tied to a show (from tmdb_extras)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT x.anilist_id, x.anime_type, e.title_romaji, e.title_english, e.start_year
                FROM tmdb_extras x
                LEFT JOIN anime_entries e ON x.anilist_id = e.anilist_id
                WHERE x.tmdb_id = %s
                ORDER BY e.start_year, x.anilist_id
            """, (tmdb_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_show_extras: {e}")
        return []

def get_show_info(tmdb_id: int) -> Dict:
    """Gets show info from tmdb_shows table."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tmdb_shows WHERE tmdb_id = %s", (tmdb_id,))
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Database error in get_show_info: {e}")
        return {}


def get_movie_info(tmdb_id: int) -> Dict:
    """Gets movie info from the tmdb_movies table (TMDB *movie* id keyed)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tmdb_movies WHERE tmdb_id = %s", (tmdb_id,))
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Database error in get_movie_info: {e}")
        return {}


def get_catalogue_items() -> List[Dict]:
    """Build the full anime catalogue from the local DB only (no external calls).

    One row per AniList entry (every season / movie / OVA we have mapped), with
    its category (anime_type) and the ids the frontend needs to navigate
    (anilist_id for /seasons, tmdb_id + season_number for /info & /watch).
    Posters come from tmdb_shows where present (lazily populated, so often null)
    — we never hit TMDB here. Sorted by title.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # anilist_id -> (tmdb_id, season_number) for real TV seasons.
            cursor.execute("SELECT anilist_id, tmdb_id, season_number FROM tmdb_seasons")
            season_map: Dict[int, Tuple[int, int]] = {}
            for r in cursor.fetchall():
                season_map.setdefault(r["anilist_id"], (r["tmdb_id"], r["season_number"]))

            # anilist_id -> tmdb_id for extras (specials/OVAs/movies).
            cursor.execute("SELECT anilist_id, tmdb_id FROM tmdb_extras")
            extra_map: Dict[int, int] = {}
            for r in cursor.fetchall():
                extra_map.setdefault(r["anilist_id"], r["tmdb_id"])

            # tmdb_id -> poster_path (sparse; only shows that were opened once).
            cursor.execute("SELECT tmdb_id, poster_path FROM tmdb_shows")
            posters: Dict[int, Optional[str]] = {r["tmdb_id"]: r["poster_path"] for r in cursor.fetchall()}

            cursor.execute(
                """SELECT anilist_id, title_romaji, title_english, title_native,
                          anime_type, start_year, genres
                   FROM anime_entries"""
            )
            entries = cursor.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_catalogue_items: {e}")
        return []

    items: List[Dict] = []
    for e in entries:
        title = e["title_english"] or e["title_romaji"] or e["title_native"]
        if not title:
            continue  # entry whose AniList titles never resolved — useless in a list
        aid = e["anilist_id"]
        tmdb_id: Optional[int] = None
        season_number: Optional[int] = None
        if aid in season_map:
            tmdb_id, season_number = season_map[aid]
        elif aid in extra_map:
            tmdb_id = extra_map[aid]
        poster_path = posters.get(tmdb_id) if tmdb_id is not None else None
        # genres is a JSON-encoded list (null for entries synced before genres
        # existed, or with no AniList genres); decode defensively to [].
        try:
            genres = json.loads(e["genres"]) if e["genres"] else []
        except (TypeError, ValueError):
            genres = []
        items.append({
            "anilist_id": aid,
            "title": title,
            "title_romaji": e["title_romaji"],
            "title_english": e["title_english"],
            "category": e["anime_type"] or "UNKNOWN",
            "genres": genres,
            "year": e["start_year"],
            "tmdb_id": tmdb_id,
            "season_number": season_number,
            "poster": _tmdb_img(poster_path) if poster_path else None,
        })

    items.sort(key=lambda x: (x["title"] or "").lower())
    return items


def _json_gzip_bodies(payload: Dict) -> Tuple[bytes, Optional[bytes]]:
    """Encode ``payload`` to JSON bytes and, when it's worth compressing, its gzip.

    Returns ``(raw_bytes, gzipped_bytes_or_None)``. Split out from ``_gzip_json`` so
    a caller that serves the same payload repeatedly (e.g. the unfiltered
    /catalogue) can cache this once and rebuild the per-request Response cheaply via
    ``_gzip_response`` instead of re-serializing + re-gzipping every time."""
    raw = orjson.dumps(payload)
    gz = gzip.compress(raw, compresslevel=6) if len(raw) >= 1024 else None
    return raw, gz


def _gzip_response(request: Request, bodies: Tuple[bytes, Optional[bytes]]) -> Response:
    """Build the JSON Response from pre-encoded ``bodies``, picking the gzip variant
    when the client accepts it and one was produced."""
    raw, gz = bodies
    headers = {"Vary": "Accept-Encoding"}
    if gz is not None and "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(content=gz, media_type="application/json", headers=headers)
    return Response(content=raw, media_type="application/json", headers=headers)


def _gzip_json(request: Request, payload: Dict) -> Response:
    """Serialize ``payload`` as JSON, gzip-compressing it when the client accepts
    gzip and the body is worth compressing. Used for the large, non-streaming
    endpoints (e.g. /catalogue) — applied per-response instead of via global
    middleware so the progressive NDJSON /watch stream is never buffered."""
    return _gzip_response(request, _json_gzip_bodies(payload))


def _is_future_air_date(air_date: Optional[str]) -> bool:
    """True when a TMDB episode air_date is strictly after today (UTC).

    TMDB air dates are bare calendar dates ('YYYY-MM-DD', no time/zone), so an
    episode airing *today* counts as aired — only a strictly-later date is "not
    yet aired". Unknown/empty/garbage dates are treated as aired so we never block
    playback on missing metadata."""
    if not air_date:
        return False
    try:
        d = datetime.strptime(air_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return d > datetime.now(timezone.utc).date()


async def _enrich_progress_rows(rows: List[Dict]) -> None:
    """Attach per-show "next episode" hints to deduped watch-progress rows so the
    frontend never offers a non-existent or not-yet-aired next episode.

    Each row is one show, carrying its latest watched season+episode. We look up
    that season's TMDB episode list (L1-cached) and add, in place:
      * season_episode_count  — total episodes in the season
      * next_episode_exists   — whether episode_number+1 is a real episode
      * next_episode_air_date — that next episode's air_date (None if n/a/unknown)

    Best-effort and concurrency-bounded; on any per-row failure the row is just
    left unannotated (the frontend then falls back to its old behaviour)."""
    sem = asyncio.Semaphore(8)

    async def _one(row: Dict) -> None:
        tmdb_id, season = row.get("tmdb_id"), row.get("season_number")
        ep = row.get("episode_number")
        if not tmdb_id or season is None:
            return
        async with sem:
            info = await _season_episode_info(int(tmdb_id), int(season))
        if not info:
            return
        row["season_episode_count"] = info.get("count")
        if ep is not None:
            air = info.get("air_dates") or {}
            nxt = int(ep) + 1
            row["next_episode_exists"] = nxt in air
            row["next_episode_air_date"] = air.get(nxt)

    await asyncio.gather(*(_one(r) for r in rows), return_exceptions=True)


# Inject the enricher into the account router (defined here so the heavy TMDB/cache
# helpers live with the rest of api.py instead of in account_engine — same
# dependency-injection pattern as set_resync_handler). Done at definition time
# because the module-level wiring near include_router runs before this point.
set_episode_enricher(_enrich_progress_rows)


async def run_single_scraper(scraper_class, tmdb_id: int, season_num: int, episode_num: int,
                             anilist_data: Dict, media_type: str = "tv") -> List:
    """Run one scraper through the unified search -> embeds pipeline.

    ``media_type`` is "tv" (the default — every existing caller) or "movie".
    Scrapers that don't declare ``SUPPORTS_MOVIES`` are skipped for movie requests
    so the title/episode-oriented anime sources never build a bogus
    season-1/episode-1 URL for a standalone film."""
    if media_type == "movie" and not getattr(scraper_class, "SUPPORTS_MOVIES", False):
        return []
    scraper = scraper_class()
    try:
        media_ctx = {
            "tmdb_id": tmdb_id,
            "tmdb_season": season_num,
            "media_type": media_type,
            **anilist_data
        }
        slug = await scraper.search_anime(media_ctx)
        if not slug:
            return []
        return await scraper.get_episode_embeds(slug, episode_num, season_num)
    except Exception as e:
        logger.error(f"Scraper error for {scraper_class.__name__}: {e}")
        return []
    finally:
        await scraper.close()

def _public_base_url(request: Request) -> str:
    """Public base URL of this backend, honoring reverse-proxy forwarded headers.

    Behind a TLS-terminating reverse proxy (our Docker deploy), uvicorn sees a
    plain HTTP request, so ``request.base_url`` reports ``http://`` — which makes
    the absolute proxy/stream URLs we emit for the operator-owned sources (Jellyfin,
    local, cache) get blocked as mixed content on the HTTPS frontend. Trust
    ``X-Forwarded-Proto``/``X-Forwarded-Host`` (set by the proxy) so the URL is
    HTTPS, regardless of uvicorn's --proxy-headers/--forwarded-allow-ips config.
    """
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        # X-Forwarded-Proto can be a comma-separated list ("https,http").
        proto = proto.split(",")[0].strip()
        return f"{proto}://{host}/"
    return str(request.base_url)

async def resolve_streams(embed_urls: List[str], base_url: str = "", language: Optional[str] = None) -> List[Dict]:
    """Resolve embed URLs to direct stream URLs.

    ``base_url`` is the public base of this backend (e.g. https://host/). It is
    used to turn a resolver's relative proxy/player path (Jellyfin, local, cache)
    into an absolute stream URL the frontend can load.

    ``language`` is an optional human-readable audio/subtitle label (e.g. the NAS
    target's dub language), known by some sources (the cache) and not others. When
    set, it is stamped onto every resolved stream so the frontend can show it;
    otherwise the streams carry no language and it stays blank.
    """
    if not embed_urls:
        return []
    
    resolver_instances = [resolver_class() for resolver_class in ALL_RESOLVERS]
    resolved_streams = []
    
    for embed_url in embed_urls:
        # Find matching resolver
        matched_resolver = None
        for resolver in resolver_instances:
            if resolver.domain_keyword in embed_url.lower():
                matched_resolver = resolver
                break
        
        if matched_resolver:
            try:
                resolved = await matched_resolver.resolve(embed_url)
                # A resolver may return a LIST of already-formed stream dicts
                # ({"url", "source", "type", optional "language"/"subtitles"}) when
                # one marker fans out to many variants (ScreenScape: a server's
                # qualities/languages). Absolutize any same-origin proxy paths and
                # append each as its own tile.
                if isinstance(resolved, list):
                    for item in resolved:
                        if not isinstance(item, dict) or not item.get("url"):
                            continue
                        item_url = item["url"]
                        if item_url.startswith("/") and base_url:
                            item_url = base_url.rstrip("/") + item_url
                        item_subs = item.get("subtitles") or None
                        if item_subs and base_url:
                            item_subs = [
                                {**s, "url": base_url.rstrip("/") + s["url"]}
                                if isinstance(s.get("url"), str) and s["url"].startswith("/")
                                else s
                                for s in item_subs
                            ]
                        stream_obj = {
                            "source": item.get("source") or matched_resolver.source_name,
                            "type": item.get("type")
                            or ("hls" if "m3u8" in item_url.lower() else "mp4"),
                            "url": item_url,
                        }
                        if item_subs:
                            stream_obj["subtitles"] = item_subs
                        if item.get("language"):
                            stream_obj["language"] = item["language"]
                        resolved_streams.append(stream_obj)
                    continue
                # A resolver may return a bare URL string (the common case) or a
                # dict {"url", "subtitles"} when it also has external subtitle
                # tracks (ShowBox/Febbox). Normalise to (url, subtitles).
                subtitles = None
                source_override = None
                if isinstance(resolved, dict):
                    subtitles = resolved.get("subtitles") or None
                    # A resolver may override the display label per-stream (the
                    # Cache source labels each stream with its NAS target's name).
                    source_override = resolved.get("source") or None
                    direct_video_url = resolved.get("url")
                else:
                    direct_video_url = resolved
                # Subtitle URLs are same-origin proxy paths too — absolutize them
                # against the backend base like the main stream URL.
                if subtitles and base_url:
                    subtitles = [
                        {**s, "url": base_url.rstrip("/") + s["url"]}
                        if isinstance(s.get("url"), str) and s["url"].startswith("/")
                        else s
                        for s in subtitles
                    ]
                if direct_video_url:
                    # Decide the stream's shape by the URL the resolver returned,
                    # NOT by source_name (which is a mutable display label):
                    #   * /{x}_proxy/h/...  -> ad-stripped player-page proxy
                    #     (Movish) -> iframe the backend page.
                    #   * /jellyfin_proxy/... -> a proxied raw stream -> hls/mp4.
                    #   * anything relative ("/..") is made absolute against the
                    #     backend base so the frontend (a different origin) loads
                    #     it from us.
                    # Resolvers that hand back an absolute third-party URL fall
                    # through to the generic hls/mp4 branch.
                    is_proxy_path = direct_video_url.startswith("/")
                    abs_url = direct_video_url
                    if is_proxy_path and base_url:
                        abs_url = base_url.rstrip("/") + direct_video_url
                    source_label = source_override or matched_resolver.source_name

                    if "_proxy/h/" in direct_video_url or direct_video_url.startswith("/player"):
                        # Backend-hosted player page (Movish player-proxy, or our
                        # /player wrapping a Jellyfin/PlayIMDb/AnimeSuge stream):
                        # the frontend just iframes it.
                        resolved_streams.append({
                            "source": source_label,
                            "type": "iframe",
                            "url": abs_url
                        })
                    else:
                        stream_type = "hls" if "m3u8" in direct_video_url.lower() else "mp4"
                        stream_obj = {
                            "source": source_label,
                            "type": stream_type,
                            "url": abs_url
                        }
                        if subtitles:
                            stream_obj["subtitles"] = subtitles
                        resolved_streams.append(stream_obj)
                else:
                    # resolve() found nothing playable. Only fall back to
                    # iframing the raw embed_url if it's a genuine http(s) embed
                    # page (legacy resolvers). For marker-based sources
                    # (crimson-playimdb:..., crimson-animesuge:..., etc.) the
                    # embed_url is an INTERNAL routing token, not a URL — iframing
                    # it yields an empty frame src that the frontend's
                    # `frame-src https:` CSP blocks ("This content is blocked").
                    # Drop the source instead so it never surfaces as a dead tile.
                    if embed_url.lower().startswith(("http://", "https://")):
                        resolved_streams.append({
                            "source": f"{matched_resolver.source_name} (Embed)",
                            "type": "iframe",
                            "url": embed_url
                        })
                    else:
                        logger.info(
                            f"{matched_resolver.source_name}: no stream for marker "
                            f"{embed_url!r}; dropping (not a frameable URL)."
                        )
                        continue
            except Exception as e:
                # A resolver that errors out has nothing playable to offer. Drop
                # it entirely instead of emitting a broken "(Error)" iframe — that
                # placeholder used to surface as a dead source (e.g. Movish, which
                # fails fast and so raced to the top of the list). Just log it.
                logger.error(f"Resolver error for {matched_resolver.source_name}: {e}")
                continue
        else:
            resolved_streams.append({
                "source": "Direct Embed",
                "type": "iframe",
                "url": embed_url
            })

    # Stamp the known language onto every stream from this batch (all embeds in a
    # single call share one language). Left off entirely when unknown.
    if language:
        for stream in resolved_streams:
            stream["language"] = language

    return resolved_streams

# --- API ENDPOINTS ---
@app.get("/")
async def root():
    """API root endpoint"""
    return {
        "version": VERSION,
        "message": "Hehe, you found me, Luminas Crimsonveil, the eternal empress of this realm. Be proud, little mortal. ✨",
    }


@app.get("/lumi")
async def lumi_blessing():
    """A little shrine to the empress. Returns a random royal blessing — used by
    the frontend's Konami-code secret page and anyone curious enough to find it.
    Public (whitelisted on the login wall) so Lumi greets even the uninvited."""
    return {
        "empress": lumi.EMPRESS,
        "title": lumi.TITLE,
        "blessing": lumi.blessing(),
        "sigil": "🦇",
    }

@app.get("/search/anime")
async def search_anime_by_name(query_name: str = Query(..., min_length=1, description="Anime name to search")):
    """Search for anime by name"""
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")
    
    try:
        async with http_client() as client:
            results = await fetch_tmdb_search_results(client, query_name)
        
        return {
            "success": True,
            "query": query_name,
            "count": len(results),
            "suggestions": results
        }
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@app.get("/trending")
async def get_trending_anime(limit: int = Query(10, ge=1, le=50, description="Number of results to return")):
    """Get trending anime"""
    try:
        async with http_client() as client:
            results = await fetch_trending_anime(client, limit)

        return {
            "success": True,
            "count": len(results),
            "animes": results
        }
    except Exception as e:
        logger.error(f"Trending error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trending anime")

# --- Non-anime TV shows (secondary surface) ---------------------------------
# Parallel to /search/anime + /trending, but for general TV shows. They reuse the
# existing TMDB-keyed playback path (/info + /watch/{tmdb_id}/{season}/{episode}),
# so no new watch/info routes are needed — only discovery + a TMDB-keyed overview.

@app.get("/search/shows")
async def search_shows_by_name(query_name: str = Query(..., min_length=1, description="TV show name to search")):
    """Search for non-anime TV shows by name (kind='show', keyed by tmdb_id)."""
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")
    try:
        async with http_client() as client:
            results = await fetch_tmdb_show_search_results(client, query_name)
        return {
            "success": True,
            "query": query_name,
            "count": len(results),
            "suggestions": results,
        }
    except Exception as e:
        logger.error(f"Show search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@app.get("/trending/shows")
async def get_trending_shows(limit: int = Query(10, ge=1, le=50, description="Number of results to return")):
    """Get trending non-anime TV shows."""
    try:
        async with http_client() as client:
            results = await fetch_trending_shows(client, limit)
        return {
            "success": True,
            "count": len(results),
            "shows": results,
        }
    except Exception as e:
        logger.error(f"Trending shows error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trending shows")

# --- General (non-anime) movies (secondary surface) -------------------------
# Parallel to /search/shows + /trending/shows, but for standalone movies. They are
# played by /watch/movie/{tmdb_id} (movies have no season/episode) and described by
# /movie-overview/{tmdb_id}.

@app.get("/search/movies")
async def search_movies_by_name(query_name: str = Query(..., min_length=1, description="Movie name to search")):
    """Search for general movies by name (kind='movie', keyed by tmdb_id)."""
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")
    try:
        async with http_client() as client:
            results = await fetch_tmdb_movie_search_results(client, query_name)
        return {
            "success": True,
            "query": query_name,
            "count": len(results),
            "suggestions": results,
        }
    except Exception as e:
        logger.error(f"Movie search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@app.get("/trending/movies")
async def get_trending_movies(limit: int = Query(10, ge=1, le=50, description="Number of results to return")):
    """Get trending general movies."""
    try:
        async with http_client() as client:
            results = await fetch_trending_movies(client, limit)
        return {
            "success": True,
            "count": len(results),
            "movies": results,
        }
    except Exception as e:
        logger.error(f"Trending movies error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trending movies")

@app.get("/catalogue")
async def get_catalogue(
    request: Request,
    category: Optional[str] = Query(None, description="Optional format filter, e.g. TV, MOVIE, OVA, ONA, SPECIAL"),
    genre: Optional[str] = Query(None, description="Optional genre filter, e.g. Action, Romance, Comedy"),
):
    """Full anime catalogue for a 'browse by category' page.

    Lists every anime in our local DB (name + format + genres + navigation ids)
    with no external API calls. ``categories`` and ``genres`` always reflect the
    whole catalogue (so the frontend can render all tabs/chips); ``animes`` is
    filtered when a ``category`` (format) and/or ``genre`` query param is given.

    The (large) response is gzip-compressed when the client accepts it.
    """
    # v2: item shape gained a `genres` field; bump so pre-genre cached lists
    # (which lack it) aren't served.
    cache_key = "catalogue:v2"
    derived_key = "catalogue:v2:derived"  # memoized full-catalogue breakdowns
    body_key = "catalogue:v2:body"        # memoized unfiltered response bodies
    # L1: in-process cache for the whole item list (no DB round-trip on a hit).
    items = _local_get(cache_key)
    derived = _local_get(derived_key)
    if items is None or derived is None:
        if items is None:
            cached = await get_cached_response(cache_key)
            if cached and "items" in cached:
                items = cached["items"]
            else:
                loop = asyncio.get_event_loop()
                items = await loop.run_in_executor(None, get_catalogue_items)
                if items:
                    await set_cached_response(cache_key, {"items": items}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
            items = items or []
            _local_set(cache_key, items)

        # Format + genre breakdowns over the FULL catalogue (before any filtering).
        # They only change when `items` is reloaded, so memoize them rather than
        # re-scanning the whole list on every request (the previous behaviour, paid
        # even on a cache hit). Recomputed whenever either L1 slot expired.
        counts: Dict[str, int] = {}
        genre_counts: Dict[str, int] = {}
        for it in items:
            counts[it["category"]] = counts.get(it["category"], 0) + 1
            for g in it.get("genres") or []:
                genre_counts[g] = genre_counts.get(g, 0) + 1
        derived = {
            "categories": [{"category": k, "count": v} for k, v in sorted(counts.items())],
            "genres": [{"genre": k, "count": v} for k, v in sorted(genre_counts.items())],
        }
        _local_set(derived_key, derived)
        # A fresh item list invalidates any cached unfiltered body.
        _local_cache.pop(body_key, None)

    # Unfiltered catalogue is by far the most-requested shape and is identical for
    # every client within a cache window — serialize + gzip it once and reuse the
    # bytes (orjson encode + level-6 gzip of the full list was the real per-request
    # cost). Filtered views (category/genre) are smaller and computed on demand.
    if not category and not genre:
        bodies = _local_get(body_key)
        if bodies is None:
            bodies = _json_gzip_bodies({
                "success": True,
                "count": len(items),
                "total": len(items),
                "categories": derived["categories"],
                "genres": derived["genres"],
                "animes": items,
            })
            _local_set(body_key, bodies)
        return _gzip_response(request, bodies)

    animes = items
    if category:
        wanted = category.strip().upper()
        animes = [it for it in animes if (it["category"] or "").upper() == wanted]
    if genre:
        wanted_g = genre.strip().casefold()
        animes = [
            it for it in animes
            if any(g.casefold() == wanted_g for g in (it.get("genres") or []))
        ]

    return _gzip_json(request, {
        "success": True,
        "count": len(animes),
        "total": len(items),
        "categories": derived["categories"],
        "genres": derived["genres"],
        "animes": animes,
    })

# Themed notice shown on an overview page when the live TMDB show fetch failed and
# the page was rebuilt from local/AniList metadata only (see _degraded_season_list
# and the /overview fallback). Phrased in Lumi's voice for the frontend banner.
DEGRADED_OVERVIEW_NOTICE = {
    "kind": "degraded",
    "title": "The Archives Flicker",
    "message": (
        "The Crimson Archives refused to answer for this title, so Lumi has rewoven "
        "this page from her own faded memory. Some seasons, episodes, or art may be "
        "missing until the archive stirs awake — try again in a little while, mortal."
    ),
}


def _degraded_season_list(tmdb_id: int) -> List[Dict]:
    """Season list built purely from the locally-stored AniList<->TMDB mapping, for
    when the live TMDB show fetch is unavailable.

    Carries enough for the frontend to render the season tabs and route the
    (anilist-keyed) play buttons, but omits the TMDB-only fields (poster, air date,
    episode count) we couldn't fetch — those simply come back null.
    """
    seasons = []
    for s in get_show_seasons(tmdb_id):
        num = s["season_number"]
        seasons.append({
            "season_number": num,
            "anilist_id": s.get("anilist_id"),
            "tmdb_id": tmdb_id,
            "tmdb_season": num,
            "name": f"Season {num}",
            "poster": None,
            "summary": None,
            "air_date": None,
            "episode_count": None,
            "title_romaji": s.get("title_romaji"),
            "title_english": s.get("title_english"),
            "anime_type": s.get("anime_type"),
        })
    return seasons


def _build_season_list(tmdb_id: int, show: Dict) -> List[Dict]:
    """Build the per-season list from TMDB's real seasons, attaching AniList mapping.

    The AniList mapping + entry titles for every season come from a single JOIN
    query (get_show_seasons) instead of two DB queries per season.
    """
    db_seasons = {s["season_number"]: s for s in get_show_seasons(tmdb_id)}
    seasons = []
    for s in show.get("seasons", []):
        num = s["season_number"]
        mapped = db_seasons.get(num, {})
        seasons.append({
            "season_number": num,
            "anilist_id": mapped.get("anilist_id"),
            "tmdb_id": tmdb_id,
            "tmdb_season": num,
            "name": s["name"],
            "poster": s["poster"] or show.get("poster"),
            "summary": s.get("overview") or show.get("overview"),
            "air_date": s["air_date"],
            "episode_count": s["episode_count"],
            "title_romaji": mapped.get("title_romaji"),
            "title_english": mapped.get("title_english"),
            "anime_type": mapped.get("anime_type"),
        })
    return seasons

@app.get("/show/{tmdb_id}")
async def get_show_details(tmdb_id: int):
    """Returns show info + every TMDB season (playable via the TMDB-keyed sources), AniList-mapped where known."""
    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    show_info = get_show_info(tmdb_id) or {
        "tmdb_id": tmdb_id,
        "title": show.get("title"),
        "overview": show.get("overview"),
        "poster_path": show.get("poster_path"),
        "backdrop_path": show.get("backdrop_path"),
        "first_air_date": show.get("first_air_date"),
    }

    return {
        "success": True,
        "show": show_info,
        "seasons": _build_season_list(tmdb_id, show),
        "extras": get_show_extras(tmdb_id)
    }

@app.get("/season/{tmdb_id}/{season_number}")
async def get_season_details(tmdb_id: int, season_number: int):
    """Combined TMDB season metadata + AniList metadata (AniList optional)."""
    anilist_id = get_anilist_id(tmdb_id, season_number)

    async with http_client() as client:
        tmdb_meta, anilist_meta = await asyncio.gather(
            fetch_tmdb_metadata(client, tmdb_id, season_number),
            fetch_anilist_metadata(client, anilist_id) if anilist_id else _empty(),
        )

    if not tmdb_meta and not anilist_meta:
        raise HTTPException(status_code=404, detail=f"No data for TMDB ID {tmdb_id} season {season_number}")

    return {
        "success": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "anilist_id": anilist_id,
        "tmdb_metadata": tmdb_meta,
        "anilist_metadata": anilist_meta
    }

def _ndjson(obj: Dict) -> str:
    """Serialize one NDJSON record: a single JSON object followed by a newline."""
    return json.dumps(obj, ensure_ascii=False) + "\n"


# Sent to the proxy + client so progressive lines actually flush through instead
# of being buffered until the response completes (nginx buffers by default).
_STREAM_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}


async def stream_watch_response(tmdb_id: int, season_number: int, episode_number: int,
                                anilist_id: Optional[int], fallback_title: Optional[str] = None,
                                base_url: str = "", media_type: str = "tv"):
    """Progressively scrape + resolve an episode, yielding NDJSON lines as each
    source is found — instead of waiting for every scraper to finish.

    ``media_type`` is "tv" (every existing caller) or "movie". For a movie there's
    no season/episode and no AniList mapping: the air-date / localized-title /
    cache-ticket steps (all TV/episode concepts) are skipped, and only the
    movie-capable TMDB-keyed sources run (see run_single_scraper).

    Emits, in order:
      * one ``{"type": "meta", ...}`` line (ids + title), flushed immediately;
      * one ``{"type": "stream", source, streamType, url}`` line per resolved
        stream, the instant its scraper + resolver finish — the sources race, so
        the fastest one reaches the player first;
      * a final ``{"type": "done", "count": N}`` line once every scraper is done.

    Works without an AniList mapping (e.g. TMDB-only seasons of long shows): the
    TMDB-keyed sources play off the TMDB id, and title-based scrapers fall back to
    the TMDB show title. ``base_url`` (the backend's public base) is threaded into stream
    resolution so the proxy sources can emit an absolute iframe URL.
    """
    anilist_data = {}
    if anilist_id:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id) or {}

    title = anilist_data.get("title") or fallback_title
    media_ctx = {**anilist_data, "title": title}

    yield _ndjson(build_meta_line(
        tmdb_id=tmdb_id,
        season_number=season_number,
        episode_number=episode_number,
        anilist_id=anilist_id,
        title=title,
    ))

    # Don't waste scraper work on an episode that hasn't aired yet. TMDB carries a
    # per-episode air_date; when the requested episode is dated in the future, tell
    # the client to render a "not yet aired" state instead of racing every scraper
    # only to resolve zero sources. Extras (specials/OVAs/movies) aren't in the
    # numbered-season episode list, so they have no air_date here and play normally.
    # Movies have no episode list at all — skip the check entirely.
    if media_type != "movie":
        ep_info = await _season_episode_info(tmdb_id, season_number)
        air_date = (ep_info.get("air_dates") or {}).get(episode_number)
        if _is_future_air_date(air_date):
            yield _ndjson(build_unaired_line(
                air_date=air_date,
                title=title,
                season_number=season_number,
                episode_number=episode_number,
            ))
            yield _ndjson(build_done_line(0))
            return

    # German streaming scrapers (s.to, aniworld) list many non-anime shows under
    # their German broadcast title — e.g. NCIS is "Navy CIS" on s.to — which TMDB
    # only exposes via /translations, so English-title matching alone misses them.
    # Feed the German title(s) in as extra search candidates. Only on the
    # no-AniList path: AniList-mapped anime already carry their own synonyms and
    # that matching stays byte-identical. Skipped for movies (that endpoint is the
    # TV /translations entity; the movie sources are TMDB-id keyed anyway).
    if not anilist_id and media_type != "movie":
        try:
            async with http_client() as client:
                german_titles = await fetch_tmdb_localized_titles(client, tmdb_id)
            if german_titles:
                existing = list(media_ctx.get("synonyms") or [])
                media_ctx["synonyms"] = existing + [
                    t for t in german_titles if t not in existing
                ]
        except Exception as e:
            logger.warning(f"localized-title enrichment failed for {tmdb_id}: {e}")

    # Each scraper runs as its own task: scrape -> resolve -> push the resolved
    # streams onto a queue the moment they're ready, so a slow source never holds
    # back a fast one. A shared seen-set (guarded by a lock) dedupes embeds and
    # stream URLs across sources, preserving the old global de-dup behaviour while
    # the work happens concurrently.
    queue: asyncio.Queue = asyncio.Queue()
    seen_embeds: set = set()
    seen_urls: set = set()
    lock = asyncio.Lock()

    async def _work(scraper_class):
        try:
            embeds = await run_single_scraper(
                scraper_class, tmdb_id, season_number, episode_number, media_ctx,
                media_type=media_type,
            )
            for embed in embeds:
                # Embeds are either a bare URL string or a {"url", "language"}
                # dict (scrapers that know the dub/sub language, e.g. aniworld).
                if isinstance(embed, dict):
                    embed_url, language = embed.get("url"), embed.get("language")
                else:
                    embed_url, language = embed, None
                if not embed_url:
                    continue
                async with lock:
                    if embed_url in seen_embeds:
                        continue
                    seen_embeds.add(embed_url)
                for stream in await resolve_streams([embed_url], base_url=base_url, language=language):
                    async with lock:
                        if stream["url"] in seen_urls:
                            continue
                        seen_urls.add(stream["url"])
                    # Server-side cache: don't download on resolve (that always
                    # caches whichever source resolves fastest, not the one the
                    # viewer picks). Instead stamp cacheable streams with a signed
                    # ticket; the player redeems it via /cache/confirm after ~10s of
                    # actual playback, and only then is the download enqueued.
                    # Movies aren't cached (the cache key is TV-shaped, tmdb/season/
                    # episode); mint_ticket owns that policy and returns None for a
                    # movie, so no ticket is emitted (no extra branch needed here).
                    stream["cacheTicket"] = await cache_manager.mint_ticket(
                        stream,
                        tmdb_id=tmdb_id,
                        season_number=season_number if season_number is not None else 0,
                        episode_number=episode_number if episode_number is not None else 0,
                        anilist_id=anilist_id,
                        media_type=media_type,
                    )
                    await queue.put(stream)
        except Exception as e:
            logger.error(f"Streaming scraper error for {scraper_class.__name__}: {e}")

    workers = [asyncio.create_task(_work(sc)) for sc in ALL_SCRAPERS]

    async def _finish():
        # Wait for every scraper, then push the sentinel that ends the drain loop.
        await asyncio.gather(*workers, return_exceptions=True)
        await queue.put(None)

    finisher = asyncio.create_task(_finish())

    count = 0
    try:
        while True:
            stream = await queue.get()
            if stream is None:  # sentinel: all scrapers finished
                break
            count += 1
            # Shape (incl. the cacheTicket-only-when-present rule) lives in
            # core.contracts so it can't drift from the client/crimson-sources.
            yield _ndjson(build_stream_line(stream))
        yield _ndjson(build_done_line(count))
    finally:
        # If the client disconnects mid-stream the generator is closed here —
        # cancel the still-running tasks so they don't leak (no-op if done).
        finisher.cancel()
        for w in workers:
            w.cancel()


@app.get("/watch/{tmdb_id}/{season_number}/{episode_number}")
@limiter.limit("30/minute")
async def get_watch_links(request: Request, tmdb_id: int, season_number: int, episode_number: int):
    """Get streaming links as a progressive NDJSON stream (one line per source,
    emitted as soon as that source resolves). Works even for TMDB seasons with no
    AniList mapping (long shows like Naruto) — the proxy sources play off the TMDB id."""
    anilist_id = get_anilist_id(tmdb_id, season_number)

    fallback_title = None
    if not anilist_id:
        info = get_show_info(tmdb_id)
        fallback_title = info.get("title") if info else None
        if not fallback_title:
            async with http_client() as client:
                show = await fetch_tmdb_show(client, tmdb_id)
            fallback_title = show.get("title")

    return StreamingResponse(
        stream_watch_response(tmdb_id, season_number, episode_number, anilist_id,
                              fallback_title, base_url=_public_base_url(request)),
        media_type="application/x-ndjson",
        headers=_STREAM_HEADERS,
    )


@app.get("/watch/movie/{tmdb_id}")
@limiter.limit("30/minute")
async def get_movie_watch_links(request: Request, tmdb_id: int):
    """Streaming links for a standalone MOVIE (TMDB *movie* id), as the same
    progressive NDJSON the TV watch route emits — one line per source. Movies have
    no season/episode and no AniList mapping; only the movie-capable TMDB-keyed
    sources run. Declared before /watch/{anilist_id}/{episode_number} so the literal
    'movie' segment is matched here rather than failing that route's int parse.

    The meta line carries null season_number/episode_number; the player ignores
    them for movies."""
    # A title helps the title-based movie source (ShowBox). Prefer the stored row,
    # then a live TMDB fetch; never hard-fail (sources can still play off the id).
    info = get_movie_info(tmdb_id)
    fallback_title = info.get("title") if info else None
    if not fallback_title:
        try:
            async with http_client() as client:
                movie = await fetch_tmdb_movie(client, tmdb_id)
            fallback_title = movie.get("title")
        except Exception as e:
            logger.warning(f"movie title fetch failed for {tmdb_id}: {e}")

    return StreamingResponse(
        stream_watch_response(tmdb_id, None, None, None,
                              fallback_title, base_url=_public_base_url(request),
                              media_type="movie"),
        media_type="application/x-ndjson",
        headers=_STREAM_HEADERS,
    )


@app.get("/scrape-meta/{tmdb_id}/{season_number}")
@limiter.limit("60/minute")
async def get_scrape_meta(request: Request, tmdb_id: int, season_number: int):
    """The title bundle the *client-side* discovery sources need to search the
    target sites (New System, Phase 1.5).

    The TS engine running in the viewer's browser (crimson-sources) resolves
    TMDB-keyed sources off the id alone, but the title-matching discovery sources
    (aniworld / s.to / stomirror / aniwatch / AnimeSuge) search by title — and the
    German broadcast synonyms come from TMDB /translations, which needs the
    server-held TMDB key (a C5 secret that must never ship to the browser). So the
    client fetches this grant and merges it into its MediaCtx, keeping title
    matching byte-identical to the backend scrapers without leaking the key.

    Returns exactly the fields the backend's own ``media_ctx`` carries (see
    ``stream_watch_response``): primary title, the AniList title variants, and the
    synonym list (AniList synonyms + German titles). Login-gated like ``/watch``,
    so anonymous users can't use it as a free metadata service."""
    anilist_id = get_anilist_id(tmdb_id, season_number)

    # Release year + IMDb id — for the year-disambiguated (hdrezka/lookmovie) and
    # IMDb-keyed (insertunit) client sources. Both cached + best-effort.
    release_year, imdb_id = await _show_year_imdb(tmdb_id)

    if anilist_id:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id) or {}
        title = anilist_data.get("title")
        synonyms = list(anilist_data.get("synonyms") or [])
        return {
            "success": True,
            "anilist_id": anilist_id,
            "title": title,
            "title_english": anilist_data.get("title_english"),
            "title_romaji": anilist_data.get("title_romaji"),
            "title_native": anilist_data.get("title_native"),
            "synonyms": synonyms,
            "release_year": release_year,
            "imdb_id": imdb_id,
        }

    # No-AniList path (TMDB-only seasons of long shows): the primary title is the
    # TMDB show title, enriched with the German broadcast title(s) the s.to-family
    # sites list non-anime shows under — exactly the enrichment the watch stream
    # does for the no-AniList case.
    info = get_show_info(tmdb_id)
    title = info.get("title") if info else None
    synonyms: List[str] = []
    try:
        async with http_client() as client:
            if not title:
                show = await fetch_tmdb_show(client, tmdb_id)
                title = show.get("title")
            german_titles = await fetch_tmdb_localized_titles(client, tmdb_id)
        synonyms = [t for t in (german_titles or []) if t]
    except Exception as e:
        logger.warning(f"scrape-meta enrichment failed for {tmdb_id}: {e}")

    return {
        "success": True,
        "anilist_id": None,
        "title": title,
        "title_english": title,
        "title_romaji": None,
        "title_native": None,
        "synonyms": synonyms,
        "release_year": release_year,
        "imdb_id": imdb_id,
    }


def _year_from_date(date_str: Optional[str]) -> Optional[int]:
    """Pull the 4-digit year off a TMDB date ("2023-07-21" -> 2023)."""
    if not date_str or len(date_str) < 4 or not date_str[:4].isdigit():
        return None
    return int(date_str[:4])


async def _show_year_imdb(tmdb_id: int) -> Tuple[Optional[int], Optional[str]]:
    """(release_year, imdb_id) for a TMDB show; best-effort, both cached."""
    try:
        async with http_client() as client:
            show = await fetch_tmdb_show(client, tmdb_id)
            imdb = await fetch_tmdb_imdb_id(client, tmdb_id, "tv")
        return _year_from_date((show or {}).get("first_air_date")), imdb
    except Exception as e:
        logger.warning(f"scrape-meta year/imdb failed for show {tmdb_id}: {e}")
        return None, None


@app.get("/scrape-meta/movie/{tmdb_id}")
@limiter.limit("60/minute")
async def get_scrape_meta_movie(request: Request, tmdb_id: int):
    """Movie twin of /scrape-meta — the title + release year + IMDb id the
    title/IMDb-keyed client sources (hdrezka/lookmovie/insertunit) need to match a
    movie. The TMDB key stays server-side (C5). Login-gated like /watch."""
    title = None
    release_year = None
    imdb_id = None
    try:
        info = get_movie_info(tmdb_id)
        title = info.get("title") if info else None
        async with http_client() as client:
            movie = await fetch_tmdb_movie(client, tmdb_id)
            if not title:
                title = (movie or {}).get("title")
            release_year = _year_from_date((movie or {}).get("release_date"))
            imdb_id = await fetch_tmdb_imdb_id(client, tmdb_id, "movie")
    except Exception as e:
        logger.warning(f"scrape-meta(movie) enrichment failed for {tmdb_id}: {e}")

    return {
        "success": True,
        "anilist_id": None,
        "title": title,
        "title_english": title,
        "title_romaji": None,
        "title_native": None,
        "synonyms": [],
        "release_year": release_year,
        "imdb_id": imdb_id,
    }


# --- crimson-proxy sign grant (New System §8a) -----------------------------
# The E2 (web-only, no-extension) path: the client resolves a stream in the
# browser and needs a *signed* crimson-proxy link to relay the segment bytes off
# the backend — but PROXY_SECRET must never ship to the browser. So the client
# sends the upstream URL(s) + the headers the CDN wants injected here, and we hand
# back the signed proxy link(s). This is the only thing that keeps PROXY_SECRET
# server-side while letting the client drive what gets fetched.
#
# Login-gated (NOT in _PUBLIC_PREFIXES) + rate-limited, so it can't be used as an
# anonymous free signing/relay oracle. We only sign http(s) upstreams; the proxy
# itself still runs its own isSafeUpstream SSRF check before fetching.
_SIGN_MAX_ITEMS = 24


def _sign_one(item: Dict) -> Optional[str]:
    """Sign a single ``{url, referer?, origin?, userAgent?}`` into a proxy link, or
    None if the url is missing / not http(s)."""
    if not isinstance(item, dict):
        return None
    url = (item.get("url") or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    return _crimson_proxy.proxy_url(
        url,
        referer=(item.get("referer") or ""),
        origin=(item.get("origin") or ""),
        user_agent=(item.get("userAgent") or item.get("user_agent") or ""),
    )


@app.post("/sign")
@limiter.limit("240/minute")
async def sign_proxy_links(request: Request):
    """Mint signed crimson-proxy link(s) for client-resolved streams (New System
    §8a). Accepts either a single ``{url, referer, origin, userAgent}`` object or
    ``{"items": [ … ]}`` for batch signing, and always returns a parallel
    ``signed`` array (null for any item we refuse to sign).

    Returns 503 when the external proxy isn't configured (no ``CRIMSON_PROXY_BASE``
    / ``PROXY_SECRET``) — the client then stays on E3 (extension) or E0 (backend),
    exactly as today, so an unconfigured proxy never breaks playback."""
    if not _crimson_proxy.is_enabled():
        return JSONResponse({"ok": False, "error": "proxy_unconfigured"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    items = body.get("items")
    if items is None:
        items = [body]  # single-object form
    if not isinstance(items, list) or not items:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)
    if len(items) > _SIGN_MAX_ITEMS:
        items = items[:_SIGN_MAX_ITEMS]

    signed = [_sign_one(it) for it in items]
    return {"ok": True, "signed": signed}


# --- client-side resolve grants (New System: take the backend out of the byte path) ---
# Some operator-owned sources can't run wholly in the viewer's browser because the
# final hop needs a server-held secret — e.g. the Jellyfin access token. But only
# the *resolve* needs the secret; the URL it yields is a stream the viewer (or the
# crimson-proxy edge) can fetch. So /resolve does the token lookup server-side and
# returns the **raw** stream URL + the headers the upstream wants — and the client
# engine delivers the bytes (extension E3 / signed crimson-proxy E2). The heavy
# mp4/HLS never travels through this backend; only a little control traffic does.
#
# It's login-gated (NOT in _PUBLIC_PREFIXES) + rate-limited, so it can't be used as
# an anonymous oracle. A source that isn't configured returns 503 and the client
# cleanly stays on the backend /watch line (same-origin proxy) for it.


async def _grant_febbox(
    tmdb_id: int, season_num: int, episode_num: int,
    anilist_data: Dict, media_type: str, base_url: str,
) -> List[Dict]:
    """Run the ShowBox discovery + the token-gated Febbox player lookup, returning
    RAW direct-file streams (not /febbox_proxy paths). Subtitle URLs stay on the
    signed proxy (tiny .srt -> WebVTT) and are absolutized here against the backend
    base. Gated on FEBBOX_UI_TOKEN (an operator-only secret), so this returns
    nothing on a stock deployment — it's wired only for the private client engine."""
    embeds = await run_single_scraper(
        ShowBoxScraper, tmdb_id, season_num, episode_num, anilist_data, media_type
    )
    if not embeds:
        return []
    resolver = FebboxResolver()
    out: List[Dict] = []
    for embed in embeds:
        try:
            streams = await resolver.resolve_direct(embed)
        except Exception as e:
            logger.warning(f"[resolve] febbox resolve_direct failed: {type(e).__name__} - {e}")
            continue
        # resolve_direct returns one stream per quality variant (best-first).
        for res in streams or []:
            if not res.get("url"):
                continue
            subs = res.get("subtitles") or []
            if base_url:
                subs = [
                    {**s, "url": base_url.rstrip("/") + s["url"]}
                    if isinstance(s.get("url"), str) and s["url"].startswith("/") else s
                    for s in subs
                ]
            out.append({
                # per-quality "ShowBox (1080p)" -> dedups with the client tile
                "label": res.get("label") or resolver.source_name,
                "streamType": res.get("streamType") or "mp4",
                "url": res["url"],
                "headers": res.get("headers") or {},
                "subtitles": subs,
                "language": res.get("language"),
            })
    return out


def _jellyfin_edge_inject_enabled() -> bool:
    """Opt-in switch for delivering Jellyfin off-backend via crimson-proxy edge
    token injection. OFF by default → Jellyfin stays fully on the backend /watch
    proxy (today's behaviour, no regression). Flip it on ONLY after the proxy is
    deployed with NITRO_JELLYFIN_HOSTS + NITRO_JELLYFIN_TOKEN, since the edge — not
    the browser — holds the token and the client path is E2-only."""
    return (os.getenv("JELLYFIN_EDGE_INJECT", "").strip().lower() in ("1", "true", "yes", "on"))


def _jellyfin_grant_configured() -> bool:
    return jellyfin_is_configured() and _jellyfin_edge_inject_enabled()


async def _grant_jellyfin(
    tmdb_id: int, season_num: int, episode_num: int,
    anilist_data: Dict, media_type: str, base_url: str,
) -> List[Dict]:
    """Resolve the Jellyfin item to its RAW, token-less absolute URL. The client
    delivers it E2-only through the crimson-proxy, which injects the access token at
    the edge — so the heavy bytes go Jellyfin → edge → viewer and the token never
    reaches the browser. ``base_url`` is unused (no same-origin proxy path here)."""
    embeds = await run_single_scraper(
        JellyfinScraper, tmdb_id, season_num, episode_num, anilist_data, media_type
    )
    if not embeds:
        return []
    resolver = JellyfinResolver()
    out: List[Dict] = []
    for embed in embeds:
        try:
            res = await resolver.resolve_direct(embed)
        except Exception as e:
            logger.warning(f"[resolve] jellyfin resolve_direct failed: {type(e).__name__} - {e}")
            continue
        if not res or not res.get("url"):
            continue
        out.append({
            "label": resolver.source_name,  # "Jellyfin" -> dedups with the /watch tile
            "streamType": res.get("streamType") or "hls",
            "url": res["url"],
            # No upstream headers: the edge supplies the token + Authorization itself.
            "headers": {},
            "subtitles": [],
            "language": None,
        })
    return out


# Per-source grant registry: source key -> (is_configured probe, runner). Add an
# operator-owned secret source here and it gains a client-delivery path for free.
_RESOLVE_GRANTS = {
    "jellyfin": (_jellyfin_grant_configured, _grant_jellyfin),
    "febbox": (febbox_is_configured, _grant_febbox),
    "showbox": (febbox_is_configured, _grant_febbox),  # alias (the display label is "ShowBox")
}


@app.post("/resolve")
@limiter.limit("120/minute")
async def resolve_grant(request: Request):
    """Server-side resolve grant for cookie/secret-bound sources (New System).

    Body is the client's MediaCtx + a ``source`` key:
    ``{source, tmdbId, mediaType, season, episode, title, titleEnglish,
    titleRomaji, titleNative, synonyms}``. Returns
    ``{ok, streams:[{label, streamType, url, headers, subtitles, language}]}`` with
    **raw** CDN URLs — the client engine handles the actual byte delivery.

    503 when the requested source isn't configured (e.g. FEBBOX_UI_TOKEN unset);
    the client then keeps using the backend /watch line for it."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    source = (body.get("source") or "").strip().lower()
    grant = _RESOLVE_GRANTS.get(source)
    if not grant:
        return JSONResponse({"ok": False, "error": "unknown_source"}, status_code=404)
    is_conf, runner = grant
    if not is_conf():
        return JSONResponse({"ok": False, "error": "source_unconfigured"}, status_code=503)

    try:
        tmdb_id = int(body.get("tmdbId") or body.get("tmdb_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    media_type = "movie" if (body.get("mediaType") or "tv") == "movie" else "tv"
    try:
        season_num = int(body.get("season") or 1)
        episode_num = int(body.get("episode") or 1)
    except (TypeError, ValueError):
        season_num, episode_num = 1, 1

    # The title bundle the discovery scraper matches on — the same fields the client
    # already carries (and enriched via /scrape-meta). None values are simply skipped
    # by the scraper's candidate-title builder.
    anilist_data = {
        "title": body.get("title"),
        "title_english": body.get("titleEnglish"),
        "title_romaji": body.get("titleRomaji"),
        "title_native": body.get("titleNative"),
        "synonyms": body.get("synonyms") or [],
    }

    base_url = _public_base_url(request)
    try:
        streams = await runner(
            tmdb_id, season_num, episode_num, anilist_data, media_type, base_url
        )
    except Exception as e:
        logger.error(f"[resolve] grant for {source!r} failed: {type(e).__name__} - {e}")
        return JSONResponse({"ok": False, "error": "resolve_failed"}, status_code=502)

    return {"ok": True, "streams": streams}


# --- movie-web bridge (/mw) -------------------------------------------------
# A thin compatibility surface that re-shapes the existing scrape+resolve
# pipeline into @movie-web/providers' native `Stream` JSON, so a modified
# movie-web fork can consume Crimson as a single "source" instead of scraping
# locally. These routes are the ONLY ones an API key can reach (see the login
# wall): a valid X-API-Key unlocks /mw and nothing else.
#
# Two differences from the frontend /watch routes:
#   * the output is one buffered JSON document (a streams[] array), not the
#     progressive NDJSON our own player consumes — movie-web's runner wants a
#     source to return its streams as a value;
#   * `iframe`-type sources (Movish player-proxy, AnimeSuge /player) are dropped:
#     movie-web has no iframe player, only direct hls/file playback. The direct
#     sources (PlayIMDb, Cinema.bz, ShowBox, VidSrc, Jellyfin, Cache, …) carry
#     through unchanged.
def _mw_slug(text: Optional[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "src"


def _mw_captions(subtitles: Optional[List[Dict]]) -> List[Dict]:
    """Map Crimson's `{label, lang, url}` subtitle tracks onto movie-web's
    `Caption` shape. URLs are already absolutized same-origin proxy paths (see
    resolve_streams), which serve WebVTT — so default the type to vtt, honoring
    an explicit .srt extension when present."""
    out: List[Dict] = []
    for i, s in enumerate(subtitles or []):
        url = s.get("url")
        if not url:
            continue
        label = s.get("label") or s.get("lang") or "Unknown"
        ctype = "srt" if ".srt" in url.lower() else "vtt"
        out.append({
            "id": f"{_mw_slug(label)}-{i}",
            "type": ctype,
            "url": url,
            "language": s.get("lang") or label,
            "hasCorsRestrictions": False,
        })
    return out


def _to_mw_stream(line: Dict, idx: int) -> Optional[Dict]:
    """One NDJSON `stream` line -> one movie-web `Stream`, or None if movie-web
    can't play it (iframe sources, or a line with no URL)."""
    stype = line.get("streamType")
    url = line.get("url")
    if not url or stype == "iframe":
        return None
    captions = _mw_captions(line.get("subtitles"))
    # `flags` is intentionally empty: it advertises no special playback
    # guarantees, so the fork routes the stream through its own proxy (which is
    # also where it injects the bridge key) rather than fetching us directly.
    base = {
        "id": f"crimson-{_mw_slug(line.get('source'))}-{idx}",
        "flags": [],
        "captions": captions,
        # Non-standard hints the fork can surface (source label + dub/sub
        # language). movie-web ignores unknown keys, so this is additive.
        "crimsonSource": line.get("source"),
        "crimsonLanguage": line.get("language"),
    }
    if stype == "hls":
        return {**base, "type": "hls", "playlist": url}
    # mp4 / any direct file: movie-web's `file` shape keys streams by quality.
    # Crimson doesn't probe quality, so expose it as the single "unknown" rung.
    return {**base, "type": "file", "qualities": {"unknown": {"type": "mp4", "url": url}}}


async def _collect_mw_streams(agen) -> Tuple[Optional[Dict], List[Dict]]:
    """Drain the NDJSON watch generator into (meta, movie-web streams[]). Reuses
    the entire real pipeline (scrape, resolve, dedup, air-date + localized-title
    handling) — this only reshapes the output, it does not re-implement it."""
    meta: Optional[Dict] = None
    streams: List[Dict] = []
    idx = 0
    async for raw in agen:
        try:
            evt = json.loads(raw)
        except Exception:
            continue
        etype = evt.get("type")
        if etype == "meta":
            meta = evt
        elif etype == "stream":
            mw = _to_mw_stream(evt, idx)
            idx += 1
            if mw:
                streams.append(mw)
        elif etype == "unaired":
            meta = {**(meta or {}), "unaired": True, "air_date": evt.get("air_date")}
    return meta, streams


@app.get("/mw/watch/movie/{tmdb_id}")
@limiter.limit("30/minute")
async def mw_watch_movie(request: Request, tmdb_id: int):
    """movie-web bridge — streams for a standalone MOVIE (TMDB movie id), as a
    single JSON document of native movie-web `Stream`s. Declared before the TV
    route so the literal 'movie' segment matches here. Requires a valid
    X-API-Key (or an admin/user session)."""
    info = get_movie_info(tmdb_id)
    fallback_title = info.get("title") if info else None
    if not fallback_title:
        try:
            async with http_client() as client:
                movie = await fetch_tmdb_movie(client, tmdb_id)
            fallback_title = movie.get("title")
        except Exception as e:
            logger.warning(f"[mw] movie title fetch failed for {tmdb_id}: {e}")

    meta, streams = await _collect_mw_streams(
        stream_watch_response(tmdb_id, None, None, None, fallback_title,
                              base_url=_public_base_url(request), media_type="movie")
    )
    return {
        "success": True,
        "media": "movie",
        "tmdb_id": tmdb_id,
        "title": (meta or {}).get("title") or fallback_title,
        "streams": streams,
    }


@app.get("/mw/watch/{tmdb_id}/{season_number}/{episode_number}")
@limiter.limit("30/minute")
async def mw_watch_tv(request: Request, tmdb_id: int, season_number: int, episode_number: int):
    """movie-web bridge — streams for a TV episode (TMDB show id + season +
    episode), as a single JSON document of native movie-web `Stream`s. Mirrors
    the frontend /watch route's id/title resolution, then reshapes the output.
    Requires a valid X-API-Key (or an admin/user session)."""
    anilist_id = get_anilist_id(tmdb_id, season_number)
    fallback_title = None
    if not anilist_id:
        info = get_show_info(tmdb_id)
        fallback_title = info.get("title") if info else None
        if not fallback_title:
            async with http_client() as client:
                show = await fetch_tmdb_show(client, tmdb_id)
            fallback_title = show.get("title")

    meta, streams = await _collect_mw_streams(
        stream_watch_response(tmdb_id, season_number, episode_number, anilist_id,
                              fallback_title, base_url=_public_base_url(request))
    )
    payload = {
        "success": True,
        "media": "tv",
        "tmdb_id": tmdb_id,
        "season": season_number,
        "episode": episode_number,
        "title": (meta or {}).get("title") or fallback_title,
        "streams": streams,
    }
    if meta and meta.get("unaired"):
        payload["unaired"] = True
        payload["air_date"] = meta.get("air_date")
    return payload


@app.post("/cache/confirm")
@limiter.limit("120/minute")
async def confirm_cache(request: Request):
    """Player calls this once the viewer has actually watched a source for a few
    seconds, passing back the ``cacheTicket`` that source carried. Only then is
    that exact stream enqueued for server-side caching — so we cache the source
    the viewer *chose* (its quality + language), not whichever resolved fastest.

    The ticket is HMAC-signed by ``/watch``, so no arbitrary URL can be injected
    into the downloader. Behind the login wall; always 200 so it never leaks
    whether caching is on or whether the episode was already cached."""
    try:
        body = await request.json()
        ticket = (body or {}).get("ticket") or ""
    except Exception:
        ticket = ""
    accepted = await cache_manager.confirm_ticket(ticket) if ticket else False
    return {"ok": bool(accepted)}


@app.post("/telemetry/resolve")
@limiter.limit("60/minute")
async def telemetry_resolve(request: Request):
    """Ingest an anonymous per-source resolve beacon from the client engine.

    Body: ``{"events": [{"source": "Cinema.bz (tcloud)", "ok": true, "env": "extension"}, …]}``.
    Strictly aggregate + anonymous — no title, no user, no IP is stored (see
    telemetry_engine). Restores the source-success visibility lost when resolving
    moved client-side. Behind the login wall + rate-limited; always 200 so a
    beacon can be fire-and-forget."""
    try:
        body = await request.json()
        events = (body or {}).get("events") or []
    except Exception:
        events = []
    rows = 0
    if isinstance(events, list) and events:
        try:
            rows = await run_in_threadpool(telemetry_store.record_batch, events)
        except Exception as e:
            logger.warning(f"telemetry ingest failed: {e}")
    return {"ok": True, "recorded": rows}


# --- CONTINUE-WATCHING WARMUP ----------------------------------------------
# When a viewer saves progress on an episode, we look ahead to the NEXT one,
# scrape+resolve it in the background, and hand the source closest to their
# language/dub-sub preference to the cache engine — so by the time they hit "next"
# it's already remuxed onto the NAS and plays instantly. The progress-upsert route
# (account_engine) calls _schedule_warmup via the injected handler; everything here
# is best-effort and fire-and-forget, and self-skips when caching is disabled.

# Don't re-scrape the same next-episode on every progress tick: progress posts fire
# every few seconds of playback, so collapse repeats for one (show, season, ep) into
# a single scrape window. The cache engine's DB claim dedupes the actual download
# regardless; this just spares the redundant scraping.
_WARMUP_TTL = 900.0          # seconds — one warmup per next-episode per 15 min
_WARMUP_MAX = 5000           # hard cap to bound memory
_warmup_seen: Dict[str, float] = {}
# Strong refs to in-flight warmup tasks so the event loop doesn't GC them mid-run.
_warmup_tasks: set = set()


async def _resolve_all_streams(tmdb_id: int, season_number: int, episode_number: int,
                               anilist_id: Optional[int], fallback_title: Optional[str],
                               base_url: str, media_type: str = "tv") -> List[Dict]:
    """Collect every resolvable stream for one episode into a list — a
    non-progressive sibling of ``stream_watch_response`` used by the warmup. Runs
    all scrapers concurrently, resolves their embeds, dedupes by embed/URL, and
    returns the streams. Best-effort: a failing scraper is skipped.

    The media-context build mirrors ``stream_watch_response`` (AniList metadata +
    German-title synonyms for the no-AniList path) so the warmup resolves the same
    sources the real /watch call would — kept deliberately in sync."""
    anilist_data = {}
    if anilist_id:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id) or {}
    title = anilist_data.get("title") or fallback_title
    media_ctx = {**anilist_data, "title": title}
    if not anilist_id and media_type != "movie":
        try:
            async with http_client() as client:
                german_titles = await fetch_tmdb_localized_titles(client, tmdb_id)
            if german_titles:
                existing = list(media_ctx.get("synonyms") or [])
                media_ctx["synonyms"] = existing + [
                    t for t in german_titles if t not in existing
                ]
        except Exception as e:
            logger.warning(f"warmup localized-title enrichment failed for {tmdb_id}: {e}")

    seen_embeds: set = set()
    seen_urls: set = set()
    out: List[Dict] = []
    lock = asyncio.Lock()

    async def _work(scraper_class):
        try:
            embeds = await run_single_scraper(
                scraper_class, tmdb_id, season_number, episode_number, media_ctx,
                media_type=media_type,
            )
            for embed in embeds:
                if isinstance(embed, dict):
                    embed_url, language = embed.get("url"), embed.get("language")
                else:
                    embed_url, language = embed, None
                if not embed_url:
                    continue
                async with lock:
                    if embed_url in seen_embeds:
                        continue
                    seen_embeds.add(embed_url)
                for stream in await resolve_streams([embed_url], base_url=base_url, language=language):
                    async with lock:
                        if stream["url"] in seen_urls:
                            continue
                        seen_urls.add(stream["url"])
                        out.append(stream)
        except Exception as e:
            logger.error(f"warmup scraper error for {scraper_class.__name__}: {e}")

    await asyncio.gather(*(_work(sc) for sc in ALL_SCRAPERS), return_exceptions=True)
    return out


def _warmup_pick_best(streams: List[Dict], preferences: Optional[Dict]) -> Optional[Dict]:
    """Pick the stream the viewer would most likely auto-play, mirroring the
    frontend ranker (crimson-client/src/hooks.js ``streamRank``): the language/
    dub-sub preference is the PRIMARY key (×1000), the global source priority
    (Cache > Voe > Jellyfin) is the tiebreaker within a language tier. Lower wins.
    With no preference set, source priority alone decides. Returns None for []."""
    prefs = preferences or {}
    pref_lang = (prefs.get("language") or "").strip().lower()
    pref_type = (prefs.get("type") or "").strip().lower()

    def _mismatch(stream: Dict) -> int:
        if not pref_lang and not pref_type:
            return 0
        tag = (stream.get("language") or "").lower()
        miss = 0
        if pref_lang and pref_lang not in tag:
            miss += 1
        if pref_type and pref_type not in tag:
            miss += 1
        return miss

    def _priority(stream: Dict) -> int:
        if "/cache_proxy/" in (stream.get("url") or ""):
            return 0
        s = (stream.get("source") or "").lower()
        if "voe" in s:
            return 1
        if "jellyfin" in s:
            return 2
        return 100

    if not streams:
        return None
    return min(streams, key=lambda s: _mismatch(s) * 1000 + _priority(s))


async def _warmup_next_episode(*, base_url: str, tmdb_id: int, season_number: int,
                               episode_number: int, preferences: Optional[Dict]) -> None:
    """Scrape+resolve the episode after the one just watched and hand the
    preference-closest cacheable source to the cache engine. Fully best-effort;
    never raises (it runs detached from the request)."""
    try:
        if tmdb_id is None or season_number is None or episode_number is None:
            return
        # Caching off? Resolving would be wasted work — bail before any scraping.
        if not await run_in_threadpool(cache_manager._store.get_enabled):
            return

        next_ep = int(episode_number) + 1

        # TTL dedupe (see _warmup_seen): one warmup per next-episode per window.
        now = time.monotonic()
        key = f"{tmdb_id}:{season_number}:{next_ep}"
        seen_until = _warmup_seen.get(key)
        if seen_until is not None and seen_until > now:
            return
        if len(_warmup_seen) >= _WARMUP_MAX:
            _warmup_seen.clear()
        _warmup_seen[key] = now + _WARMUP_TTL

        # The next episode must actually exist in the season and already have aired.
        info = await _season_episode_info(int(tmdb_id), int(season_number))
        air = info.get("air_dates") or {}
        if next_ep not in air:
            return  # end of season (or unknown episode list) — nothing to warm
        if _is_future_air_date(air.get(next_ep)):
            return  # not out yet

        # Resolve the AniList mapping the same way /watch does (same season as the
        # episode just watched, so the mapping is identical). Falls back to a TMDB
        # title for the title-based scrapers when the season isn't AniList-mapped.
        anilist_id = get_anilist_id(int(tmdb_id), int(season_number))
        fallback_title = None
        if not anilist_id:
            show = get_show_info(int(tmdb_id))
            fallback_title = show.get("title") if show else None
            if not fallback_title:
                try:
                    async with http_client() as client:
                        meta = await fetch_tmdb_show(client, int(tmdb_id))
                    fallback_title = meta.get("title")
                except Exception:
                    pass

        streams = await _resolve_all_streams(
            int(tmdb_id), int(season_number), next_ep, anilist_id,
            fallback_title, base_url=base_url, media_type="tv",
        )
        # Only weigh sources the cache engine would actually accept (enabled +
        # ffmpeg present + tappable, non-self URL) so we pick the best *cacheable*
        # match rather than a source we'd silently fail to cache.
        cacheable = [s for s in streams if await cache_manager._cacheable(s)]
        best = _warmup_pick_best(cacheable, preferences)
        if not best:
            return

        await cache_manager.maybe_enqueue(
            best,
            tmdb_id=int(tmdb_id),
            season_number=int(season_number),
            episode_number=next_ep,
            anilist_id=int(anilist_id) if anilist_id is not None else None,
            media_type="tv",
        )
        logger.info(
            f"warmup: queued next episode for caching tmdb={tmdb_id} "
            f"s{season_number}e{next_ep} source={best.get('source')!r} "
            f"lang={best.get('language')!r}"
        )
    except Exception as e:
        logger.warning(f"continue-watching warmup failed: {e}")


def _schedule_warmup(request: Request, *, tmdb_id: int, season_number: int,
                     episode_number: int, preferences: Optional[Dict]) -> None:
    """Account router's warmup hook: fire the warmup as a detached background task
    (keeping a strong ref so it isn't GC'd) and return immediately, so saving watch
    progress is never delayed by it. The public base URL is captured from the
    request here (where the forwarded-header logic lives) for the proxy sources."""
    base_url = _public_base_url(request)
    task = asyncio.create_task(_warmup_next_episode(
        base_url=base_url, tmdb_id=tmdb_id, season_number=season_number,
        episode_number=episode_number, preferences=preferences,
    ))
    _warmup_tasks.add(task)
    task.add_done_callback(_warmup_tasks.discard)


set_warmup_handler(_schedule_warmup)


# --- ADMIN: RUNTIME / SYSTEM SNAPSHOT --------------------------------------
# Powers the dashboard's expanded "System" view. Lives here (not admin_routes)
# because it reads VERSION + the scraper/resolver registries + the warm pool;
# injected into the admin router via set_system_handler (same DI pattern as the
# resync handler). Every DB-touching call hops the threadpool so we never block
# the event loop.

def _human_duration(seconds: float) -> str:
    """Compact uptime string, e.g. '3d 04h 12m'."""
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


async def _admin_system_info() -> Dict:
    """A rich runtime snapshot for one replica: version + uptime, registry sizes,
    capability flags, DB-pool utilisation, and the server-side cache aggregate."""
    pool = await run_in_threadpool(pool_stats)
    cache_enabled = await run_in_threadpool(cache_store.get_enabled)
    cache_stats = await run_in_threadpool(cache_store.stats)
    cache_targets = await run_in_threadpool(cache_store.enabled_targets)
    local_sources = await run_in_threadpool(local_source_store.list_sources)
    local_enabled = sum(1 for s in local_sources if s.get("enabled"))
    # Live-ping the external CORS proxies (if any) so the dashboard shows which
    # are up. Cheap GET / health check per host; off entirely when unconfigured.
    # Uses refresh_health (not bare probe_bases) so opening the dashboard also
    # updates the routing health cache that drives automatic failover in proxy_url.
    proxy_hosts = await _crimson_proxy.refresh_health()

    now = time.time()
    return {
        "version": VERSION,
        "started_at": datetime.fromtimestamp(_PROCESS_STARTED_AT, timezone.utc).isoformat(),
        "uptime_seconds": int(now - _PROCESS_STARTED_AT),
        "uptime_human": _human_duration(now - _PROCESS_STARTED_AT),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "registry": {
            "scrapers": len(ALL_SCRAPERS),
            "resolvers": len(ALL_RESOLVERS),
        },
        "flags": {
            "require_login": bool(getattr(Config, "REQUIRE_LOGIN", False)),
            "jellyfin_configured": jellyfin_is_configured(),
            "local_configured": local_is_configured(),
            "cache_enabled": bool(cache_enabled),
            "ffmpeg_available": ffmpeg_available(),
            "tmdb_key_set": bool(getattr(Config, "TMDB_API_KEY", None)),
            "rate_limit_storage": os.getenv("RATE_LIMIT_STORAGE_URI", "memory://"),
            "github_token_set": bool(os.getenv("GITHUB_TOKEN")),
            "crimson_proxy_enabled": _crimson_proxy.is_enabled(),
        },
        "proxies": {
            "enabled": _crimson_proxy.is_enabled(),
            "secret_set": bool(os.getenv("PROXY_SECRET")),
            "routed_sources": _crimson_proxy.ROUTED_SOURCES,
            "hosts": proxy_hosts,
        },
        "db_pool": pool,
        "cache": {
            "enabled": bool(cache_enabled),
            "targets_enabled": len(cache_targets),
            **cache_stats,
        },
        "local_sources": {"total": len(local_sources), "enabled": local_enabled},
    }


set_system_handler(_admin_system_info)


# --- ADMIN: SOURCE HEALTH ---------------------------------------------------
# Probe every external scrape source against a known canary title (the real
# search→embeds pipeline, so green == would actually play), and report the
# operator-provided library sources' configuration. Results are cached for a few
# minutes so flipping to the dashboard tab doesn't re-hammer every upstream; the
# dashboard's "Re-probe" button passes force=True. Injected via set_source_health_handler.
_SOURCE_HEALTH_TTL = float(os.getenv("SOURCE_HEALTH_TTL", "300"))
_source_health_cache: Dict[str, object] = {"at": 0.0, "data": None}
_source_health_lock = asyncio.Lock()


async def _probe_scrape_source(scraper_class, anilist_data: Dict) -> Dict:
    """End-to-end probe of one external source against the canary. Returns a row
    with status (ok/empty/error/disabled), latency, embed count and a human note."""
    name = scraper_class.__name__
    meta = source_health.meta_for(name)
    entry = {
        "id": name,
        "label": meta["label"],
        "category": "scrape",
        "note": meta.get("note"),
        "base_url": getattr(scraper_class, "BASE_URL", None),
        "supports_movies": bool(getattr(scraper_class, "SUPPORTS_MOVIES", False)),
        "latency_ms": None,
        "embeds": 0,
    }
    gate = meta.get("env_gate")
    if gate and not os.getenv(gate):
        entry.update(status="disabled", detail=f"{gate} not configured — source is dormant")
        return entry

    c = source_health.CANARY
    t0 = time.perf_counter()
    try:
        embeds = await run_single_scraper(
            scraper_class, c["tmdb_id"], c["season"], c["episode"], anilist_data,
            media_type="tv",
        )
        entry["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        n = len(embeds or [])
        entry["embeds"] = n
        if n > 0:
            entry.update(status="ok", detail=f"Resolved {n} embed(s) for the canary")
        else:
            entry.update(status="empty", detail="Reachable, but found no embeds for the canary title")
    except Exception as e:
        entry["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        entry.update(status="error", detail=(str(e)[:240] or e.__class__.__name__))
    return entry


async def _probe_library_sources() -> List[Dict]:
    """Config/occupancy status for the operator-provided sources (cache, local,
    Jellyfin). These only hold what the operator added, so they're reported by
    'is it set up and does it hold anything' rather than the canary probe."""
    out: List[Dict] = []

    cache_enabled = await run_in_threadpool(cache_store.get_enabled)
    cstats = await run_in_threadpool(cache_store.stats)
    targets = await run_in_threadpool(cache_store.enabled_targets)
    ready = cstats.get("ready") or 0
    if not cache_enabled:
        c_status, c_detail = "disabled", "Caching is switched off"
    elif ready > 0:
        c_status, c_detail = "active", f"{ready} episode(s) ready · {len(targets)} target(s)"
    else:
        c_status, c_detail = "idle", f"Enabled · {len(targets)} target(s), nothing cached yet"
    out.append({
        "id": "CacheScraper", "label": "Server Cache", "category": "library",
        "note": source_health.meta_for("CacheScraper").get("note"),
        "status": c_status, "detail": c_detail, "latency_ms": None,
        "metrics": {
            "ready": cstats.get("ready"), "pending": cstats.get("pending"),
            "downloading": cstats.get("downloading"), "failed": cstats.get("failed"),
            "targets": len(targets),
        },
    })

    local_sources = await run_in_threadpool(local_source_store.list_sources)
    enabled = [s for s in local_sources if s.get("enabled")]
    if not local_sources:
        l_status, l_detail = "disabled", "No local directories registered"
    elif enabled:
        l_status, l_detail = "active", f"{len(enabled)} of {len(local_sources)} directory(ies) enabled"
    else:
        l_status, l_detail = "idle", f"{len(local_sources)} directory(ies) registered, all disabled"
    out.append({
        "id": "LocalScraper", "label": "Local Media", "category": "library",
        "note": source_health.meta_for("LocalScraper").get("note"),
        "status": l_status, "detail": l_detail, "latency_ms": None,
        "metrics": {"total": len(local_sources), "enabled": len(enabled)},
    })

    jelly = jellyfin_is_configured()
    out.append({
        "id": "JellyfinScraper", "label": "Jellyfin", "category": "library",
        "note": source_health.meta_for("JellyfinScraper").get("note"),
        "status": "active" if jelly else "disabled",
        "detail": "Configured via JELLYFIN_* env" if jelly else "JELLYFIN_* env not set",
        "latency_ms": None, "metrics": {},
    })
    return out


async def _do_source_health() -> Dict:
    """Run the full probe sweep: shared canary metadata once, then every scrape
    source concurrently, plus the library sources. Assembles the summary tally."""
    anilist_data: Dict = {}
    try:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, source_health.CANARY["anilist_id"]) or {}
    except Exception as e:
        logger.warning(f"source-health canary metadata fetch failed: {e}")
    if not anilist_data.get("title"):
        anilist_data = {**anilist_data, "title": source_health.CANARY["title"]}

    scrape_classes = [
        c for c in ALL_SCRAPERS
        if source_health.meta_for(c.__name__)["category"] == "scrape"
    ]
    scrape_results = await asyncio.gather(
        *(_probe_scrape_source(c, anilist_data) for c in scrape_classes),
        return_exceptions=False,
    )
    library_results = await _probe_library_sources()
    sources = library_results + list(scrape_results)

    summary = {"total": len(sources)}
    for s in sources:
        summary[s["status"]] = summary.get(s["status"], 0) + 1
    # Latency stats over the scrape probes that actually ran.
    lats = [s["latency_ms"] for s in scrape_results if s.get("latency_ms") is not None]
    summary["avg_latency_ms"] = round(sum(lats) / len(lats)) if lats else None
    summary["slowest_ms"] = max(lats) if lats else None

    return {
        "canary": dict(source_health.CANARY),
        "sources": sources,
        "summary": summary,
    }


async def _admin_source_health(force: bool = False) -> Dict:
    """Cached wrapper around the probe sweep (TTL ``SOURCE_HEALTH_TTL``). The lock
    collapses a thundering herd of dashboard loads into a single sweep."""
    now = time.monotonic()
    cached = _source_health_cache.get("data")
    if not force and cached and (now - float(_source_health_cache["at"]) < _SOURCE_HEALTH_TTL):
        return {**cached, "cached": True}

    async with _source_health_lock:
        now = time.monotonic()
        cached = _source_health_cache.get("data")
        if not force and cached and (now - float(_source_health_cache["at"]) < _SOURCE_HEALTH_TTL):
            return {**cached, "cached": True}
        data = await _do_source_health()
        data["probed_at"] = datetime.now(timezone.utc).isoformat()
        _source_health_cache["at"] = time.monotonic()
        _source_health_cache["data"] = data
        return {**data, "cached": False}


set_source_health_handler(_admin_source_health)


@app.get("/anilist/{anilist_id}")
async def get_anilist_mapping(anilist_id: int):
    """Returns { tmdb_id, season_number } for an anilist_id."""
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")
    
    return {
        "success": True,
        "anilist_id": anilist_id,
        "tmdb_id": mapping[0],
        "season_number": mapping[1]
    }

# --- COMPATIBILITY ENDPOINTS (legacy frontend contract) ---
@app.get("/info/{tmdb_id}")
async def get_anime_info(tmdb_id: int, season: int = Query(1, ge=1, description="TMDB season number")):
    """Merged TMDB + AniList metadata for a (tmdb_id, season). Flat legacy shape.

    AniList is optional: seasons of long shows with no AniList entry still return
    TMDB metadata + a TMDB-derived episode list, and the description always falls
    back (AniList -> TMDB season -> TMDB show overview).
    """
    anilist_id = get_anilist_id(tmdb_id, season)

    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)
        # Season metadata (reusing the show we just fetched) and AniList metadata
        # are independent — fetch them concurrently instead of in series.
        tmdb_data, anilist_data = await asyncio.gather(
            fetch_tmdb_metadata(client, tmdb_id, season, show=show),
            fetch_anilist_metadata(client, anilist_id) if anilist_id else _empty(),
        )

    if not show and not tmdb_data and not anilist_data:
        raise HTTPException(status_code=404, detail=f"No data for TMDB ID {tmdb_id} season {season}")

    available_seasons = [s["season_number"] for s in show.get("seasons", [])]
    if not available_seasons:
        available_seasons = [s["season_number"] for s in get_show_seasons(tmdb_id)]

    # Never return an empty description / episode list.
    description = anilist_data.get("description") or tmdb_data.get("summary") or show.get("overview")

    # Prefer TMDB's per-season episode list. It is correctly split by season (with
    # real per-episode titles, thumbnails, air dates and overviews) and matches the
    # episode numbering the proxy sources actually play by. AniList's
    # streamingEpisodes are crowd-sourced and unreliable for sequel seasons — they
    # frequently echo the *first* season's titles (e.g. the Overlord II/III/IV
    # entries all return season 1's episode names), which made every season of a
    # show look identical. So AniList is only a fallback when TMDB has no
    # per-episode data for the season.
    tmdb_eps = tmdb_data.get("episodes") or []
    anilist_eps = anilist_data.get("episodes_list") or []
    episodes_list = tmdb_eps or anilist_eps

    return {
        **tmdb_data,
        **anilist_data,
        "success": True,
        "tmdb_id": tmdb_id,
        "anilist_id": anilist_id,
        "current_season": season,
        "available_seasons": available_seasons,
        "description": description,
        "summary": tmdb_data.get("summary") or show.get("overview"),
        "episodes_list": episodes_list,
        "title": anilist_data.get("title") or show.get("title"),
    }

@app.get("/watch/{anilist_id}/{episode_number}")
@limiter.limit("30/minute")
async def deprecated_watch(request: Request, anilist_id: int, episode_number: int, season_part: int = Query(1)):
    """
    Watch by anilist_id. TV seasons redirect to the canonical /watch route;
    extras (specials/OVAs/movies) have no TMDB season number, so they are served
    directly here.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id, season_number = mapping
    # Serve the stream directly rather than 301-redirecting to the canonical
    # 3-segment route. A redirect is fatal on WebKit (all iOS browsers + Safari):
    # it drops the Authorization header when fetch() follows the redirect, so the
    # redirected request hits the login wall unauthenticated → 401 → the client
    # clears the session and the user is bounced to the login wall. Extras
    # (special/OVA/movie) have no numbered season — use season 1 for URL builders.
    return StreamingResponse(
        stream_watch_response(tmdb_id, season_number if season_number is not None else 1,
                              episode_number, anilist_id,
                              base_url=_public_base_url(request)),
        media_type="application/x-ndjson",
        headers=_STREAM_HEADERS,
    )

# --- SAME-ORIGIN STREAM PROXIES ------------------------------------------
# Several sources hand the player a same-origin proxy path instead of a raw CDN
# URL, because the CDN gates segments on a Referer/Origin/UA/ASN the viewer's
# browser can't satisfy (or serves no usable CORS). Every proxy ends the same
# way: turn the resolver proxy_fetch result (status, content_type, headers,
# payload) into the right response — buffered bytes for a rewritten HLS playlist,
# a streamed body (Range/length headers forwarded) for a media segment. That tail
# lived copy-pasted in ~10 routes; it lives here once now.
def _proxy_response(status, content_type, headers, payload, *, forward_bytes_headers=False):
    """Shape a resolver ``proxy_fetch`` result into a Response/StreamingResponse.

    ``payload`` is either rewritten ``bytes`` (an HLS playlist) or an async byte
    iterator (a streamed media segment). Bytes responses don't forward upstream
    headers unless ``forward_bytes_headers`` is set (Jellyfin needs them)."""
    if isinstance(payload, (bytes, bytearray)):
        return Response(
            content=payload,
            status_code=status,
            media_type=content_type,
            headers=headers if forward_bytes_headers else None,
        )
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# NOTE: the third-party stream proxies that used to live here (the Movish ad-free
# page proxy, the table-driven signed proxies for PlayIMDb/VOE/Vidmoly/VidSrc/
# Cinema.bz/AnimeSuge, and the ScreenScape signed proxy) were removed along with
# their scrapers/resolvers. Third-party stream relaying is now handled client-side
# via the crimson-proxy CORS relay (see /sign + New_System.md). What remains are the
# operator-owned proxies below (Jellyfin, local, cache) plus the signed Febbox
# subtitle relay that backs the operator-only /resolve grant.


# --- JELLYFIN PROXY ("jellyfin" source) ---
@app.api_route("/jellyfin_proxy/{path:path}", methods=["GET", "POST"])
async def jellyfin_proxy(request: Request, path: str):
    """Authenticated reverse proxy to the user's Jellyfin server. Injects the
    access token server-side (so it never reaches the browser) and rewrites HLS
    playlists to flow back through this proxy; media segments / direct files are
    streamed straight through with Range passthrough. Configured via the
    JELLYFIN_* env vars (see resolvers.jellyfin)."""
    body = await request.body() if request.method == "POST" else None
    try:
        result = await jellyfin_proxy_fetch(
            path=path,
            query_string=request.url.query,
            method=request.method,
            body=body,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Jellyfin proxy upstream error for {path}: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")
    # Jellyfin forwards upstream headers on buffered (playlist) responses too.
    return _proxy_response(*result, forward_bytes_headers=True)


# --- FEBBOX SUBTITLE PROXY (operator-only /resolve grant) ---
# Not part of the public /watch pipeline. The /resolve grant returns Febbox's video
# URLs RAW (the client delivers those bytes itself via E3/E2); only the tiny .srt
# subtitles are minted as signed /febbox_proxy paths, which this route fetches and
# converts to WebVTT. HMAC-signed (no open relay) and inert unless FEBBOX_UI_TOKEN
# is configured.
@app.get("/febbox_proxy", include_in_schema=False)
async def febbox_proxy(request: Request):
    try:
        result = await febbox_proxy_fetch(
            url=request.query_params.get("u"),
            sig=request.query_params.get("s"),
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Febbox proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")
    return _proxy_response(*result)


# Optional same-origin stream relays for any overlaid module that ships a
# ``proxy_fetch`` (present only when the build-time overlay added it). Each mirrors
# /febbox_proxy: schema-hidden, with the HMAC verification / host allow-list living
# inside the module's own ``proxy_fetch``. A base build has none, so nothing is added.
# Routes are derived from the module names + the wiring shape from each fetch
# signature, so this file names no overlaid source.
def _register_overlay_stream_proxies():
    global _DYNAMIC_PUBLIC_PREFIXES
    import importlib
    import inspect
    import pkgutil

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


# --- LOCAL SOURCE PROXY ("Local" source: admin-registered dirs / NAS) ---
@app.get("/local_proxy/{token}")
async def local_proxy(token: str):
    """Stream a browser-playable file from an admin-registered local source.

    ``token`` is an opaque base64url of the absolute path the LocalScraper found.
    ``safe_resolve`` maps it back to a real file ONLY when it currently lives
    inside an *enabled* source root (path traversal / symlink escapes / disabled
    sources all resolve to None → 404), re-checked on every request. Starlette's
    FileResponse handles HTTP Range requests, so the player can seek."""
    real_path = await run_in_threadpool(local_safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real_path, media_type=local_media_type(real_path))


@app.get("/local_hls/{token}/{resource}")
async def local_hls(token: str, resource: str):
    """On-the-fly HLS for a transcodable Local file (mkv/avi/ts/…) whose source has
    encoding enabled — the non-direct-play counterpart of /local_proxy.

    ``resource`` is either the VOD playlist (``master.m3u8``/``media.m3u8``) or a
    segment (``seg{n}.ts``). ``safe_resolve_transcode`` re-validates on EVERY request
    that the token maps to a transcodable file inside a *currently enabled* source
    root with **encoding on** — so disabling the source (or just its encoding) instantly
    404s its transcode streams, exactly like /local_proxy for direct play. Gated by the
    login wall (NOT a public prefix), so the player must carry the session token; the
    bytes never leave this host's library unauthenticated."""
    real_path = await run_in_threadpool(local_safe_resolve_transcode, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")

    duration = await run_in_threadpool(local_transcode.probe_duration, real_path)
    if not duration:
        raise HTTPException(status_code=422, detail="Could not probe media")

    if resource in ("master.m3u8", "media.m3u8", "index.m3u8"):
        playlist = local_transcode.build_media_playlist(duration)
        return Response(content=playlist, media_type="application/vnd.apple.mpegurl")

    if resource.startswith("seg") and resource.endswith(".ts"):
        try:
            index = int(resource[3:-3])
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found")
        if index < 0 or index >= local_transcode.segment_count(duration):
            raise HTTPException(status_code=404, detail="Not found")
        data, err = await local_transcode.transcode_segment(real_path, index)
        if data is None:
            logger.warning(f"[local_hls] segment {index} failed for {real_path!r}: {err}")
            raise HTTPException(status_code=502, detail="Transcode failed")
        return Response(content=data, media_type="video/mp2t")

    raise HTTPException(status_code=404, detail="Not found")


@app.get("/cache_proxy/{token}")
async def cache_proxy(token: str):
    """Stream a server-side-cached episode straight off the NAS.

    ``token`` is an opaque base64url of the cached file's absolute path.
    ``cache_safe_resolve`` maps it back to a real file ONLY when it currently
    lives inside an *enabled* cache target (traversal/symlink escapes / disabled
    targets all 404), re-checked per request. FileResponse handles Range so the
    player can seek. Mirrors /local_proxy."""
    real_path = await run_in_threadpool(cache_safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real_path, media_type=cache_media_type(real_path))


# --- BACKEND-HOSTED PLAYER (Crimson-themed hls.js/mp4 player) ---
@app.get("/player")
async def player(
    src: str = Query(..., description="Same-origin stream path to play"),
    stream_type: str = Query("", alias="type", description="hls or mp4 (inferred if omitted)"),
    title: str = Query("", description="Optional title"),
):
    """Serve a Crimson-themed player for a same-origin proxied stream. Resolvers
    that return a raw hls/mp4 stream (e.g. Jellyfin) wrap it in this page so the
    frontend can iframe it like any other source. ``src`` is restricted to
    same-origin relative paths to prevent embedding arbitrary external content."""
    if not is_safe_src(src):
        raise HTTPException(status_code=400, detail="Invalid src (must be a same-origin path)")
    html = render_player(src=src, stream_type=stream_type, title=title)
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/seasons/{anilist_id}")
async def get_anime_seasons(anilist_id: int):
    """All seasons of the show this anilist_id belongs to (legacy shape).

    Each season carries its own tmdb_id + tmdb_season so the frontend can drill
    into /info/{tmdb_id}?season={tmdb_season} and /watch/{anilist_id}/{episode}.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id = mapping[0]

    async with http_client() as client:
        show, anime_info = await asyncio.gather(
            fetch_tmdb_show(client, tmdb_id),
            fetch_anilist_metadata(client, anilist_id),
        )
    if not show:
        raise HTTPException(status_code=404, detail="Show not found on TMDB")

    seasons_data = _build_season_list(tmdb_id, show)

    title = (anime_info or {}).get("title") or show.get("title") or "Unknown Anime"

    return {
        "success": True,
        "anilist_id": anilist_id,
        "title": title,
        "total_seasons": len(seasons_data),
        "seasons": seasons_data,
        "extras": get_show_extras(tmdb_id),
    }

@app.get("/overview/{anilist_id}")
async def get_anime_overview(anilist_id: int):
    """Aggregated show overview for the per-anime landing/overview page.

    Returns show-level metadata (title, poster, backdrop, synopsis, status, year)
    plus the full season list + extras in a single round-trip, so the frontend can
    paint the season/episode browser shell without a /seasons -> /info waterfall.

    Per-season episode lists (with the stored per-episode titles/thumbnails) are
    still fetched lazily by the frontend via /info/{tmdb_id}?season=, so /overview
    never fans out into one TMDB season call per season.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id = mapping[0]

    async with http_client() as client:
        show, anime_info = await asyncio.gather(
            fetch_tmdb_show(client, tmdb_id),
            fetch_anilist_metadata(client, anilist_id),
        )

    anime_info = anime_info or {}

    # TMDB-down fallback: if the live show fetch failed (e.g. TMDB 502s on a single
    # broken record), don't hard-404 the whole page. As long as we have *some*
    # metadata — AniList and/or the locally-stored tmdb_shows row — rebuild a
    # degraded overview from what we have and flag it so the frontend can say so.
    degraded = not show
    if degraded:
        stored = get_show_info(tmdb_id)
        if not stored and not anime_info:
            raise HTTPException(status_code=404, detail="Show not found on TMDB")
        show = {
            "title": stored.get("title"),
            "overview": stored.get("overview"),
            "poster": _tmdb_img(stored.get("poster_path")),
            "backdrop": _tmdb_img(stored.get("backdrop_path"), "original"),
            "first_air_date": stored.get("first_air_date"),
            "seasons": [],
        }
        seasons_data = _degraded_season_list(tmdb_id)
    else:
        seasons_data = _build_season_list(tmdb_id, show)

    title = anime_info.get("title") or show.get("title") or "Unknown Anime"

    # Year: prefer TMDB's first-air-date, fall back to AniList's start year.
    year = None
    first_air = show.get("first_air_date")
    if first_air:
        year = first_air[:4]
    elif (anime_info.get("start_date") or {}).get("year"):
        year = str(anime_info["start_date"]["year"])

    return {
        "success": True,
        "anilist_id": anilist_id,
        "tmdb_id": tmdb_id,
        "title": title,
        "title_romaji": anime_info.get("title_romaji"),
        "title_english": anime_info.get("title_english"),
        # AniList cover art is higher quality; fall back to the TMDB poster.
        "poster": anime_info.get("cover") or show.get("poster"),
        "backdrop": show.get("backdrop"),
        "banner": anime_info.get("banner"),
        # `description` may contain AniList HTML; `summary` is the plain TMDB overview.
        "description": anime_info.get("description") or show.get("overview"),
        "summary": show.get("overview"),
        "status": anime_info.get("status"),
        "year": year,
        "total_episodes": anime_info.get("total_episodes"),
        "total_seasons": len(seasons_data),
        # Genres from the local anime DB (same source as the catalogue). Anime-only;
        # the show-overview twin omits this, so genre tags stay anime-specific.
        "genres": get_anime_genres(anilist_id),
        "seasons": seasons_data,
        "extras": get_show_extras(tmdb_id),
        # When TMDB was unavailable, this page was rebuilt from local/AniList data
        # only; the frontend renders DEGRADED_OVERVIEW_NOTICE as a themed banner.
        "degraded": degraded,
        "notice": DEGRADED_OVERVIEW_NOTICE if degraded else None,
    }

@app.get("/show-overview/{tmdb_id}")
async def get_show_overview(tmdb_id: int):
    """Aggregated overview for a NON-ANIME TV show, keyed by tmdb_id.

    The TMDB-keyed twin of /overview/{anilist_id}: same response shape (so the
    frontend can render it with the shared overview UI), but built purely from
    TMDB — there is no AniList entry for a general show. Seasons come from TMDB's
    real season list via _build_season_list (any anilist_id fields are simply
    null), and per-season episodes are still fetched lazily by the frontend via
    /info/{tmdb_id}?season=. Playback uses /watch/{tmdb_id}/{season}/{episode}.
    """
    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)

    # TMDB-down fallback (twin of /overview): rebuild from the locally-stored
    # tmdb_shows row instead of hard-404ing when the live fetch failed. Shows have
    # no AniList entry, so the stored row is the only fallback source.
    degraded = not show
    if degraded:
        stored = get_show_info(tmdb_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Show not found on TMDB")
        stored_genres = []
        if stored.get("genres"):
            try:
                stored_genres = json.loads(stored["genres"]) or []
            except (TypeError, ValueError):
                stored_genres = []
        show = {
            "title": stored.get("title"),
            "overview": stored.get("overview"),
            "poster": _tmdb_img(stored.get("poster_path")),
            "backdrop": _tmdb_img(stored.get("backdrop_path"), "original"),
            "first_air_date": stored.get("first_air_date"),
            "genres": stored_genres,
            "seasons": [],
        }
        seasons_data = _degraded_season_list(tmdb_id)
    else:
        seasons_data = _build_season_list(tmdb_id, show)
    year = (show.get("first_air_date") or "")[:4] or None

    return {
        "success": True,
        "kind": "show",
        "anilist_id": None,
        "tmdb_id": tmdb_id,
        "title": show.get("title"),
        "title_romaji": None,
        "title_english": show.get("title"),
        "poster": show.get("poster"),
        "backdrop": show.get("backdrop"),
        "banner": None,
        "description": show.get("overview"),
        "summary": show.get("overview"),
        "status": None,
        "year": year,
        "total_episodes": None,
        "total_seasons": len(seasons_data),
        "seasons": seasons_data,
        # Genre tags — the non-anime twin of /overview's genres (from tmdb_shows).
        "genres": show.get("genres") or [],
        # General shows carry no AniList specials/OVAs/movies mapping.
        "extras": [],
        # When TMDB was unavailable, this page was rebuilt from local data only.
        "degraded": degraded,
        "notice": DEGRADED_OVERVIEW_NOTICE if degraded else None,
    }

@app.get("/movie-overview/{tmdb_id}")
async def get_movie_overview(tmdb_id: int):
    """Aggregated overview for a standalone MOVIE, keyed by its TMDB *movie* id.

    The movie twin of /show-overview: same overall response shape so the frontend
    reuses the shared overview UI, but with no seasons (movies have none) — instead
    a single ``play`` descriptor the page links to /watch-movie. Built purely from
    TMDB (movies have no AniList entry); falls back to the locally-stored
    tmdb_movies row when the live TMDB fetch fails, exactly like /show-overview.
    """
    async with http_client() as client:
        movie = await fetch_tmdb_movie(client, tmdb_id)

    degraded = not movie
    if degraded:
        stored = get_movie_info(tmdb_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Movie not found on TMDB")
        stored_genres = []
        if stored.get("genres"):
            try:
                stored_genres = json.loads(stored["genres"]) or []
            except (TypeError, ValueError):
                stored_genres = []
        movie = {
            "title": stored.get("title"),
            "overview": stored.get("overview"),
            "poster": _tmdb_img(stored.get("poster_path")),
            "backdrop": _tmdb_img(stored.get("backdrop_path"), "original"),
            "release_date": stored.get("release_date"),
            "runtime": None,
            "genres": stored_genres,
            "vote_average": None,
            "status": None,
        }
    year = (movie.get("release_date") or "")[:4] or None

    return {
        "success": True,
        "kind": "movie",
        "anilist_id": None,
        "tmdb_id": tmdb_id,
        "title": movie.get("title"),
        "title_romaji": None,
        "title_english": movie.get("title"),
        "poster": movie.get("poster"),
        "backdrop": movie.get("backdrop"),
        "banner": None,
        "description": movie.get("overview"),
        "summary": movie.get("overview"),
        "status": movie.get("status"),
        "year": year,
        # Movie-specific extras the overview UI can show if it wants to.
        "runtime": movie.get("runtime"),
        "genres": movie.get("genres") or [],
        "vote_average": movie.get("vote_average"),
        # No seasons/episodes for a movie; the page plays the single feature.
        "total_episodes": None,
        "total_seasons": 0,
        "seasons": [],
        "extras": [],
        # The single playable item — the frontend links this to /watch-movie/{id}.
        "play": {"tmdb_id": tmdb_id, "media_type": "movie"},
        "degraded": degraded,
        "notice": DEGRADED_OVERVIEW_NOTICE if degraded else None,
    }

# --- HEALTH CHECK ENDPOINT ---
@app.get("/config")
async def public_config():
    """Public, unauthenticated feature flags the frontend needs *before* login.

    Notably ``demo_mode``: on a demo deployment the login page drops the invite-code
    requirement (signup is open) and can show a "data resets nightly" hint. Booleans
    only — no secrets, counts, or paths leak through this (it's reachable without a
    session, whitelisted in _PUBLIC_EXACT)."""
    return {
        "demo_mode": Config.DEMO_MODE,
        "require_login": Config.REQUIRE_LOGIN,
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM anime_entries")
            count = cursor.fetchone()["n"]
        
        return {
            "status": "healthy",
            "database": "connected",
            "entries_count": count,
            "scrapers_available": len(ALL_SCRAPERS),
            "resolvers_available": len(ALL_RESOLVERS),
            "jellyfin_configured": jellyfin_is_configured(),
            "local_sources_configured": local_is_configured()
        }
    except Exception as e:
        # Log the real cause server-side; don't leak DB/internal detail to an
        # unauthenticated probe. Surface specifics only when DEBUG is set.
        logger.error(f"Health check failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e) if os.getenv("DEBUG") else "database unavailable",
            },
        )

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