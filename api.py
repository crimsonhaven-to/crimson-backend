"""The FastAPI assembler: the "brain, not pipe" entrypoint.

Creates the app, mounts the middleware (login wall, CORS, Lumi header), owns the
lifespan (schedulers, DB init, warm caches), registers the optional overlay's
stream proxies, wires the exception handlers and includes the routers. The
endpoints and their logic live in the ``web`` package; see web/__init__.py.
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
from core import migrations
from core import logging_setup
from core import observability
from core.db_pool import close_pool
from core.http_client import (
    open_client as open_http_client,
    close_client as close_http_client,
)
from core.response_cache import purge_expired_cache
from cache_engine.downloader import manager as cache_manager
from download_engine.manager import manager as download_manager
from resolvers import _crimson_proxy

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
from supporters_engine import router as supporters_router, store as supporters_store
from changelog_engine import router as changelog_router, service as changelog_service
from recommend_engine import router as recommend_router
from chat_engine import router as chat_router, store as chat_store
from subtitles_engine import router as subtitles_router, service as subtitles_service
from skiptimes_engine import router as skiptimes_router
from manga_engine import manga_router
from iptv_engine import (
    router as iptv_router,
    service as iptv_service,
    enabled as iptv_enabled,
)
from metadata_engine import maintenance as metadata_maintenance
from metadata_engine import sync_status

# The HTTP layer: singletons, injected engine handlers and routers.
from web.context import (
    db_engine,
    local_source_store,
    cache_store,
    download_store,
    telemetry_store,
)
from web.pipeline import _enrich_progress_rows
from web.warmup import schedule_warmup
from web.admin_handlers import admin_source_health, admin_system_info, forced_resync
from web.routes import all_routers
from web.routes.proxies import _proxy_response

# Same output as a bare basicConfig, plus the per-request correlation id and an
# opt-in JSON format. See core/logging_setup.py.
logging_setup.configure(level=logging.INFO)
logger = logging.getLogger(__name__)


# Defensive: core.config already loads its own.
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the schedulers, databases and warm caches; drain them on shutdown."""
    logger.info("Starting up FastAPI application...")

    # Presence only, never values, so a dark source is diagnosable from the boot
    # log at a glance.
    config_report.log_report(logger)

    # Kept warm for the whole process lifetime.
    open_http_client()

    # Idempotent, so safe on every replica.
    db_engine.init_db()
    # None of these tables are touched by a mapping resync.
    account_store.init_db()
    security_audit.init_db()
    apikey_store.init_db()
    supporters_store.init_db()
    local_source_store.init_db()
    cache_store.init_db()
    download_store.init_db()
    telemetry_store.init_db()

    # After the init_db()s, which still own the pre-migration baseline. Takes the
    # same advisory lock, so concurrent boots serialize. Non-fatal by design: a
    # failure is loud in the log and on /health, but must not turn a bookkeeping
    # problem into a boot loop across every replica.
    try:
        migrations.apply_pending(logger)
    except Exception as e:
        logger.error(f"Schema migrations failed: {e}", exc_info=True)

    # Here rather than at import, so merely importing the app (a test, the openapi
    # export) never wires up something that reads the DB.
    observability.install_state_collector()
    if not observability.PROMETHEUS_AVAILABLE:
        logger.info("prometheus_client not installed; /metrics is inert (503)")

    # Idempotent, and only promotes accounts that already exist, so the operator
    # reaches /admin without hand-editing the DB.
    if Config.ADMIN_EMAILS:
        try:
            promoted = account_store.bootstrap_admins(Config.ADMIN_EMAILS)
            if promoted:
                logger.info(f"Promoted {promoted} account(s) to admin from ADMIN_EMAILS")
        except Exception as e:
            logger.error(f"Admin bootstrap failed: {e}")

    # One per replica. It always owns the cheap housekeeping; the heavy Fribb
    # resync is added on exactly one replica.
    scheduler = BackgroundScheduler()

    # Rows are already deleted on access, but a challenge that is requested and
    # never completed would pile up until the next restart.
    def _purge_expired():
        try:
            account_store.purge_expired()
        except Exception as e:
            logger.error(f"Expired session/challenge purge failed: {e}")
        # Consume-on-read never deletes these, and every unique search query
        # writes one, so the table would grow unbounded.
        try:
            n = purge_expired_cache()
            if n:
                logger.info(f"Purged {n} expired api_cache rows")
        except Exception as e:
            logger.error(f"Expired api_cache purge failed: {e}")
        # An idempotent DELETE, so several replicas sweeping on their own clocks
        # is fine.
        try:
            n = security_audit.purge_old()
            if n:
                logger.info(f"Purged {n} security events past retention")
        except Exception as e:
            logger.error(f"Security event purge failed: {e}")

    scheduler.add_job(
        _purge_expired,
        trigger=IntervalTrigger(hours=6),
        id="purge_expired_job",
        replace_existing=True,
    )

    # Grouped with the purge above rather than pinned to one replica because, like
    # the other retention sweeps, it is an idempotent DELETE by timestamp: extra
    # replicas cost a redundant no-op query, not correctness.
    def _prune_chat():
        try:
            removed = chat_store.prune()
            if removed["conversations"] or removed["usage"]:
                logger.info(
                    f"Chat prune: {removed['conversations']} conversation(s), "
                    f"{removed['usage']} usage row(s)"
                )
        except Exception as e:
            logger.error(f"Chat prune failed: {e}")

    scheduler.add_job(
        _prune_chat,
        trigger=IntervalTrigger(hours=12),
        id="chat_prune_job",
        replace_existing=True,
    )

    # Every replica keeps its own copy, and ETag conditional requests keep the
    # refresh near-free against GitHub's rate limit. The initial warm-up runs off
    # the event loop, so an unreachable GitHub never delays startup.
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
        logger.info("GITHUB_TOKEN not set, /changelog will return 503 until configured")

    # Per replica, like the changelog. The warm-up is a ~25 MB JSON pull, so it
    # runs off the event loop; upstream publishes daily, so the refresh interval
    # matches. Routes also self-heal by kicking a refresh when asked while stale.
    if iptv_enabled():
        async def _warm_iptv():
            try:
                await run_in_threadpool(iptv_service.refresh)
                logger.info("IPTV catalogue warmed from iptv-org")
            except Exception as e:
                logger.error(f"Initial IPTV warm-up failed (will retry on schedule): {e}")

        asyncio.create_task(_warm_iptv())  # fire-and-forget

        def _refresh_iptv():
            try:
                iptv_service.refresh()
            except Exception as e:
                logger.error(f"IPTV catalogue refresh failed: {e}")

        scheduler.add_job(
            _refresh_iptv,
            trigger=IntervalTrigger(hours=12),
            id="iptv_refresh_job",
            replace_existing=True,
        )
    else:
        logger.info("IPTV_ENABLED=false, the Live TV surface is dark")

    # Every replica keeps its own, since each routes independently. Probing every
    # host lets proxy_url route only to the ones that are up, giving automatic
    # failover between the deploys. A couple of GETs per host, when configured.
    if _crimson_proxy.is_enabled():
        async def _warm_proxy_health():
            try:
                await _crimson_proxy.refresh_health()
                logger.info("CORS proxy health cache warmed")
            except Exception as e:
                logger.error(f"Initial proxy health probe failed (will retry on schedule): {e}")

        asyncio.create_task(_warm_proxy_health())  # must not delay startup

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
        logger.info("CRIMSON_PROXY_BASE not set, external CORS proxy disabled, /sign returns 503")

    if subtitles_service.configured():
        logger.info("OpenSubtitles configured, /subtitles is enabled")
    else:
        logger.info("OPENSUBTITLES_API_KEY not set, /subtitles will return 503 until configured")

    # Signup is open in demo mode, so all non-admin data is wiped nightly to bound
    # growth. Pinned to the single sync replica so replicas don't race the DELETE.
    if Config.DEMO_MODE:
        logger.warning(
            "DEMO_MODE is ON: signup invite gate is bypassed, non-admin data resets "
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
            logger.info("DEMO_MODE: this replica is not RUN_DB_SYNC, the nightly reset runs on the sync replica")

    # The resync rebuilds the mapping tables wholesale, so exactly one replica
    # owns it. Otherwise every replica downloads and rebuilds in lockstep, wasting
    # bandwidth and contending on the shared DB.
    if not Config.RUN_DB_SYNC:
        logger.info("RUN_DB_SYNC is disabled, this replica will not run the mapping resync")
        sync_status.set_phase("disabled", "RUN_DB_SYNC is off on this replica")
    else:
        # Fire-and-forget, so uvicorn and /health come up immediately rather than
        # blocking boot on a multi-minute download and enrichment. That matters
        # most in single-replica dev, where the one container is also the sync
        # replica. sync_database_async HEADs the Fribb URL first and returns
        # "up_to_date" when the stored ETag still matches a non-empty DB, so a warm
        # DB pays only that conditional HEAD.
        #
        # Pushed onto a worker thread, the same shape the scheduled job uses, so
        # the heavy synchronous writes never stall the loop now serving requests.
        async def _initial_sync():
            sync_status.set_phase("running", "Fribb mapping sync started", started=True)
            try:
                result = await run_in_threadpool(
                    lambda: asyncio.run(db_engine.sync_database_async())
                )
            except Exception as e:
                sync_status.set_phase("failed", str(e), finished=True)
                logger.error(f"Initial database sync failed: {e}")
                return

            if result == "up_to_date":
                sync_status.set_phase("up_to_date", "Mappings already up-to-date", finished=True)
                logger.info("Initial mapping sync: DB already up-to-date, nothing rebuilt")
            elif result == "synced":
                sync_status.set_phase("done", "Mapping tables rebuilt from Fribb", finished=True)
                logger.info("Initial database sync completed (tables rebuilt)")
            else:
                # sync_database_async already logged the cause, and the previous
                # snapshot is intact.
                sync_status.set_phase("failed", result or "unknown outcome", finished=True)
                logger.warning(f"Initial database sync did not rebuild (outcome={result})")

        asyncio.create_task(_initial_sync())  # runs off the boot path

        # BackgroundScheduler runs jobs in a worker thread with no running event
        # loop, so the job spins up its own.
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

    # All pinned to the single sync replica, so exactly one container churns this
    # much metadata. Three pieces:
    #   1. a nightly slice refresh, since nothing upstream reports a TMDB change,
    #      so the catalogue is swept oldest-first over a full cycle of nights
    #   2. a short-interval drainer for backfill jobs the dashboard queues, which
    #      arrive through a table because the serving replica cannot reach api-sync
    #   3. an optional one-shot backfill at startup
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

        # Polled often so an admin-triggered backfill starts promptly. A run can
        # take minutes, but max_instances=1 skips overlapping ticks so they cannot
        # stack.
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

            asyncio.create_task(_run_backfill())  # paced internally

    scheduler.start()
    logger.info("Background scheduler started")
    app.state.scheduler = scheduler

    # Only the dedicated cache-worker runs the ffmpeg loop; api replicas just mint
    # tickets and claim rows. The job lives in Postgres, so a download survives an
    # api redeploy and any worker can drain the queue.
    if Config.RUN_CACHE_WORKER:
        await cache_manager.start_worker()
    else:
        logger.info(
            "RUN_CACHE_WORKER disabled, this replica mints/claims cache rows but "
            "does not download (the cache-worker service does)"
        )

    # The same split as the cache worker: only the download-worker submits and
    # polls, while other replicas write pending rows and issue pause/resume.
    if Config.RUN_DOWNLOAD_WORKER:
        await download_manager.start_worker()
    else:
        logger.info(
            "RUN_DOWNLOAD_WORKER disabled, this replica queues downloads but does "
            "not run the aria2 poll loop (the download-worker service does)"
        )

    yield

    # Shutdown
    logger.info("Shutting down...")
    await cache_manager.stop()
    await download_manager.stop()
    if getattr(app.state, 'scheduler', None) is not None:
        app.state.scheduler.shutdown()
    await close_http_client()
    close_pool()
    logger.info("Shutdown complete")


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
