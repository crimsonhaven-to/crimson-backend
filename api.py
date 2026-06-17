import asyncio
import os
import gzip
import hashlib
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple
from contextlib import asynccontextmanager

import httpx
import json
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.requests import Request
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Import all scrapers & resolvers + metadata engine
from scrapers import ALL_SCRAPERS
from resolvers import ALL_RESOLVERS
from resolvers.movish import proxy_fetch as movish_proxy_fetch
from resolvers.playimdb import proxy_fetch as playimdb_proxy_fetch
from resolvers.voe import proxy_fetch as voe_proxy_fetch
from resolvers.vidmoly import proxy_fetch as vidmoly_proxy_fetch
from resolvers.vidsrc import proxy_fetch as vidsrc_proxy_fetch
from resolvers.animesuge import proxy_fetch as animesuge_proxy_fetch
from resolvers.cinemabz import proxy_fetch as cinemabz_proxy_fetch
from resolvers.febbox import proxy_fetch as febbox_proxy_fetch
from resolvers.jellyfin import proxy_fetch as jellyfin_proxy_fetch, is_configured as jellyfin_is_configured
from local_engine.db import LocalSourceStore
from local_engine.fs import (
    safe_resolve as local_safe_resolve,
    media_type_for as local_media_type,
    is_configured as local_is_configured,
)
from player import render_player, is_safe_src
from metadata_engine.db_handler import MappingDatabaseEngine
from account_engine import router as account_router, store as account_store
from account_engine.admin_routes import router as admin_router, set_resync_handler
from supporters_engine import router as supporters_router, store as supporters_store
from changelog_engine import router as changelog_router, service as changelog_service
from db_pool import get_pool, close_pool
from rate_limit import limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.concurrency import run_in_threadpool

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Single source of truth for the API version — fed to both the FastAPI app
# metadata (OpenAPI/docs) and the "/" root greeting.
VERSION = "6.1.7"

# Admin-managed local media sources (the "Local" direct-play source). The store
# is schema-init'd in lifespan; the scraper/resolver read the enabled roots
# directly via their own LocalSourceStore (the enabled-roots cache is class-wide).
local_source_store = LocalSourceStore()


def _utcnow_iso() -> str:
    """Current UTC time as a naive ISO-8601 string.

    ``datetime.utcnow()`` is deprecated (and slated for removal), so we derive
    UTC from a tz-aware ``now`` but drop the offset to keep the exact same
    ``YYYY-MM-DDTHH:MM:SS.ffffff`` shape the api_cache rows were written with —
    so lexicographic ``expires_at`` comparisons stay correct across an upgrade.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

# Load environment variables
load_dotenv()

# Configuration
class Config:
    TMDB_API_KEY = os.getenv("TMDB_API_KEY")
    # Mapping + accounts now live in PostgreSQL; the connection is configured via
    # DATABASE_URL / POSTGRES_* and pooled in db_pool (no per-process DB path).
    CACHE_TTL_SECONDS = 86400  # 24 hours
    TRENDING_CACHE_TTL_SECONDS = 21600  # 6 hours
    MAX_CONCURRENT_REQUESTS = 10
    REQUEST_TIMEOUT = 30.0
    MAX_RETRIES = 3
    RETRY_BACKOFF_FACTOR = 1.0

    # Only the replica with this set to true runs the periodic Fribb resync.
    # The sync rebuilds the mapping tables wholesale, so running it on every
    # replica is wasteful — keep it enabled on exactly one replica (see README
    # "Deploying to Docker Swarm").
    RUN_DB_SYNC = os.getenv("RUN_DB_SYNC", "true").lower() not in ("0", "false", "no")

    # Emails promoted to admin on startup (comma-separated). Seeds the first
    # admin so the /admin dashboard is reachable without hand-editing the DB;
    # afterwards admins can promote others from the dashboard itself. Only takes
    # effect for accounts that already exist (it never creates one).
    ADMIN_EMAILS = [
        e.strip() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
    ]

    # Site-wide login wall. When true (default) every content endpoint requires a
    # valid session bearer token (see the require_login middleware); a small set
    # of paths — auth, health, the signed stream proxies/player that media
    # elements load without headers, and the Ko-fi webhook — stay public. Set to
    # false to revert to a fully open API.
    REQUIRE_LOGIN = os.getenv("REQUIRE_LOGIN", "true").lower() not in ("0", "false", "no")

    # CORS Origins. Overridable via the ALLOWED_ORIGINS env var (comma-separated)
    # so the deploy can lock these down without a code change; falls back to the
    # built-in dev + crimsonhaven.to list.
    _DEFAULT_ORIGINS = [
        "https://crimsonhaven.to",
        "https://www.crimsonhaven.to",
    ]
    ALLOWED_ORIGINS = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
    ] or _DEFAULT_ORIGINS

    @classmethod
    def validate(cls):
        if not cls.TMDB_API_KEY:
            raise ValueError("TMDB_API_KEY environment variable is not set")

Config.validate()

# TMDB Headers
TMDB_HEADERS = {
    "Authorization": f"Bearer {Config.TMDB_API_KEY}",
    "accept": "application/json"
}

# Initialize database engine (storage is the shared PostgreSQL pool; see db_pool)
db_engine = MappingDatabaseEngine(tmdb_api_key=Config.TMDB_API_KEY)

# --- SHARED HTTP CLIENT ---
# One process-wide AsyncClient (opened in lifespan) instead of a fresh
# httpx.AsyncClient() per request. Reusing it keeps the TCP+TLS connections to
# TMDB / AniList warm across requests rather than paying a new handshake every
# call — the single biggest latency win on the metadata endpoints. Call sites use
# the ``http_client()`` context manager below, which yields this shared instance
# and deliberately does NOT close it on block exit.
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating a transient fallback if the
    lifespan hasn't run yet (only possible outside the normal request path)."""
    if _http_client is None:
        return httpx.AsyncClient(timeout=Config.REQUEST_TIMEOUT)
    return _http_client


@asynccontextmanager
async def http_client():
    """Yield the shared AsyncClient. Drop-in for ``httpx.AsyncClient()`` at the
    existing ``async with ... as client:`` call sites — but the shared client is
    kept open (not closed) when the block exits."""
    yield get_http_client()


# --- IN-PROCESS L1 CACHE (hot global keys) ---
# A tiny TTL cache in front of the PostgreSQL api_cache for the few hot keys that
# use a fixed global cache key (trending, catalogue). It removes a DB round-trip
# on every hit. Stateless-friendly: it only ever serves data up to its short TTL
# and each replica converges independently (no cross-replica invalidation needed
# because these payloads are read-mostly and already TTL-bounded upstream).
_LOCAL_CACHE_TTL = 300  # seconds
_local_cache: Dict[str, Tuple[float, object]] = {}


def _local_get(key: str):
    hit = _local_cache.get(key)
    if not hit:
        return None
    expiry, value = hit
    if expiry < time.monotonic():
        _local_cache.pop(key, None)
        return None
    return value


def _local_set(key: str, value: object, ttl: int = _LOCAL_CACHE_TTL) -> None:
    _local_cache[key] = (time.monotonic() + ttl, value)


# --- LIFESPAN MANAGEMENT ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    # Startup
    logger.info("Starting up FastAPI application...")

    # Open the shared HTTP client (kept warm for the whole process lifetime).
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=Config.REQUEST_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    )

    # Initialize databases (idempotent — safe on every replica).
    db_engine.init_db()
    account_store.init_db()  # account tables (untouched by mapping resyncs)
    supporters_store.init_db()  # Ko-fi supporters ledger (also resync-safe)
    local_source_store.init_db()  # admin-managed local media sources (resync-safe)

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

    scheduler.start()
    logger.info("Background scheduler started")
    app.state.scheduler = scheduler

    yield

    # Shutdown
    logger.info("Shutting down...")
    if getattr(app.state, 'scheduler', None) is not None:
        app.state.scheduler.shutdown()
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
    close_pool()  # drain the PostgreSQL connection pool
    logger.info("Shutdown complete")

# Create FastAPI app with lifespan
app = FastAPI(
    title="Anime Streaming API",
    description="API for streaming anime with multi-season support",
    version=VERSION,
    lifespan=lifespan
)

# Rate limiting (slowapi). Registered on app.state so the @limiter.limit
# decorators on the expensive/abusable endpoints take effect; the 429 handler
# returns a clean JSON error with Retry-After.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
_PUBLIC_EXACT = {"/", "/health", "/openapi.json", "/docs", "/redoc"}
_PUBLIC_PREFIXES = (
    "/auth/",
    "/kofi/webhook",
    "/changelog",
    "/player",
    "/movish_proxy",
    "/playimdb_proxy",
    "/voe_proxy",
    "/vidmoly_proxy",
    "/vidsrc_proxy",
    "/cinemabz_proxy",
    "/febbox_proxy",
    "/animesuge_proxy",
    "/jellyfin_proxy",
    "/docs",
)

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
        ):
            return await self.app(scope, receive, send)

        token = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                val = value.decode("latin-1")
                if val[:7].lower() == "bearer ":
                    token = val.split(" ", 1)[1].strip()
                break

        if await _session_is_valid(token):
            return await self.app(scope, receive, send)

        response = JSONResponse(
            {"detail": "Authentication required", "success": False},
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

def get_first_anilist_ids(tmdb_ids: List[int]) -> Dict[int, int]:
    """Map each tmdb_id -> its lowest-numbered season's anilist_id, in ONE query.

    Replaces calling ``get_show_seasons`` once per search/trending result (an N+1
    that borrowed a pooled connection per item). A tmdb_id with no mapped season
    is simply absent from the result.
    """
    if not tmdb_ids:
        return {}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT tmdb_id, anilist_id, season_number
                   FROM tmdb_seasons
                   WHERE tmdb_id = ANY(%s)
                   ORDER BY tmdb_id, season_number""",
                (list(tmdb_ids),),
            )
            out: Dict[int, int] = {}
            for r in cursor.fetchall():
                out.setdefault(r["tmdb_id"], r["anilist_id"])  # first = lowest season
            return out
    except Exception as e:
        logger.error(f"Database error in get_first_anilist_ids: {e}")
        return {}

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

def upsert_show_info(show: Dict) -> None:
    """Persist TMDB show details fetched on demand (lazy population of tmdb_shows)."""
    if not show.get("tmdb_id"):
        return
    try:
        def _write():
            with get_db_connection() as conn:
                conn.execute("""
                    INSERT INTO tmdb_shows
                        (tmdb_id, title, overview, poster_path, backdrop_path, first_air_date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tmdb_id) DO UPDATE SET
                        title=EXCLUDED.title, overview=EXCLUDED.overview,
                        poster_path=EXCLUDED.poster_path, backdrop_path=EXCLUDED.backdrop_path,
                        first_air_date=EXCLUDED.first_air_date
                """, (
                    show.get("tmdb_id"),
                    show.get("title"),
                    show.get("overview"),
                    show.get("poster_path"),
                    show.get("backdrop_path"),
                    show.get("first_air_date"),
                ))
        _write()
    except Exception as e:
        logger.error(f"Database error in upsert_show_info: {e}")

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


# --- CACHE HELPER FUNCTIONS ---
async def get_cached_response(cache_key: str) -> Optional[Dict]:
    """Retrieve cached response from database"""
    try:
        def _query():
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT response_json FROM api_cache WHERE cache_key = %s AND expires_at > %s",
                    (cache_key, _utcnow_iso())
                )
                row = cursor.fetchone()
                return json.loads(row["response_json"]) if row else None
        
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)
    except Exception as e:
        logger.error(f"Cache retrieval error for key {cache_key}: {e}")
        return None

async def set_cached_response(cache_key: str, data: Dict, ttl_seconds: int = Config.CACHE_TTL_SECONDS):
    """Save response to cache"""
    if not data:
        return
    
    try:
        expires_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl_seconds)).isoformat()
        payload = json.dumps(data)
        
        def _insert():
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO api_cache (cache_key, response_json, expires_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        response_json=EXCLUDED.response_json, expires_at=EXCLUDED.expires_at
                """, (cache_key, payload, expires_at))
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _insert)
    except Exception as e:
        logger.error(f"Cache storage error for key {cache_key}: {e}")

def purge_expired_cache() -> int:
    """Delete expired api_cache rows. Returns the number removed."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_cache WHERE expires_at < %s", (_utcnow_iso(),))
        return cursor.rowcount or 0

async def _empty() -> Dict:
    """A coroutine that resolves to ``{}`` — lets us ``asyncio.gather`` an
    optional fetch (e.g. AniList when there's no mapping) without branching."""
    return {}

def _gzip_json(request: Request, payload: Dict) -> Response:
    """Serialize ``payload`` as JSON, gzip-compressing it when the client accepts
    gzip and the body is worth compressing. Used for the large, non-streaming
    endpoints (e.g. /catalogue) — applied per-response instead of via global
    middleware so the progressive NDJSON /watch stream is never buffered."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Vary": "Accept-Encoding"}
    if len(body) >= 1024 and "gzip" in request.headers.get("accept-encoding", "").lower():
        body = gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return Response(content=body, media_type="application/json", headers=headers)

# --- TMDB API FUNCTIONS ---
async def fetch_with_retry(client: httpx.AsyncClient, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """Fetch data from API with retry logic"""
    for attempt in range(Config.MAX_RETRIES):
        try:
            response = await client.get(url, headers=TMDB_HEADERS, params=params, timeout=Config.REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:  # Rate limit
                wait_time = Config.RETRY_BACKOFF_FACTOR * (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 1}")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.warning(f"TMDB API error: Status {response.status_code} for URL {url}")
                return None
                
        except httpx.TimeoutException:
            logger.warning(f"Timeout on attempt {attempt + 1} for {url}")
            if attempt == Config.MAX_RETRIES - 1:
                return None
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
        except Exception as e:
            logger.error(f"Request error on attempt {attempt + 1}: {e}")
            if attempt == Config.MAX_RETRIES - 1:
                return None
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
    
    return None

# Bump when the cached TMDB payload shape changes, so stale entries in the
# volume-persisted api_cache are ignored after a deploy instead of served.
TMDB_CACHE_VERSION = "v2"

def _tmdb_img(path: Optional[str], size: str = "w500") -> Optional[str]:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None

async def fetch_tmdb_show(client: httpx.AsyncClient, tmdb_id: int) -> Dict:
    """
    Fetch a TMDB show with its real season list (the authority for what the
    TMDB-keyed sources can play). Cached, and persists core fields into tmdb_shows
    on first fetch.
    """
    cache_key = f"tmdb:show:{TMDB_CACHE_VERSION}:{tmdb_id}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    data = await fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}")
    if not data:
        return {}

    seasons = []
    for s in data.get("seasons", []):
        num = s.get("season_number")
        # Skip specials (season 0) and empty placeholder seasons.
        if num is None or num < 1 or (s.get("episode_count") or 0) < 1:
            continue
        seasons.append({
            "season_number": num,
            "name": s.get("name") or f"Season {num}",
            "episode_count": s.get("episode_count"),
            "air_date": s.get("air_date"),
            "poster": _tmdb_img(s.get("poster_path")),
            "overview": s.get("overview"),
        })

    result = {
        "tmdb_id": tmdb_id,
        "title": data.get("name") or data.get("original_name"),
        "overview": data.get("overview"),
        "poster_path": data.get("poster_path"),
        "backdrop_path": data.get("backdrop_path"),
        "poster": _tmdb_img(data.get("poster_path")),
        "backdrop": _tmdb_img(data.get("backdrop_path"), "original"),
        "first_air_date": data.get("first_air_date"),
        "seasons": seasons,
    }

    upsert_show_info({k: result.get(k) for k in
                      ("tmdb_id", "title", "overview", "poster_path", "backdrop_path", "first_air_date")})
    await set_cached_response(cache_key, result)
    return result

async def fetch_tmdb_metadata(client: httpx.AsyncClient, tmdb_id: int, season: int = 1,
                              show: Optional[Dict] = None) -> Dict:
    """Fetch metadata + episode list for a specific TMDB season.

    Falls back to show-level overview when the season overview is empty (common
    for anime) so a description is always available. ``show`` may be passed in by
    a caller that already fetched it (e.g. /info), avoiding a redundant cached
    re-fetch; otherwise it is fetched here.
    """
    cache_key = f"tmdb:meta:{TMDB_CACHE_VERSION}:{tmdb_id}:s{season}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    data = await fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}")
    if show is None:
        show = await fetch_tmdb_show(client, tmdb_id)  # cached

    if not data:
        logger.info(f"Season {season} not found for TMDB ID {tmdb_id}, falling back to show metadata")
        result = {
            "summary": show.get("overview"),
            "poster": show.get("poster"),
            "backdrop": show.get("backdrop"),
            "season_name": f"Season {season}",
            "air_date": None,
            "episodes": [],
        }
    else:
        episodes = [{
            "episode_number": ep.get("episode_number"),
            "title": ep.get("name") or f"Episode {ep.get('episode_number')}",
            "thumbnail": _tmdb_img(ep.get("still_path")),
            "overview": ep.get("overview"),
            "air_date": ep.get("air_date"),
            "url": None,
        } for ep in data.get("episodes", [])]

        result = {
            "summary": data.get("overview") or show.get("overview"),
            "poster": _tmdb_img(data.get("poster_path")) or show.get("poster"),
            "backdrop": show.get("backdrop"),
            "season_name": data.get("name") or f"Season {season}",
            "air_date": data.get("air_date"),
            "episodes": episodes,
        }

    if result:
        await set_cached_response(cache_key, result)

    return result

async def fetch_anilist_metadata(client: httpx.AsyncClient, anilist_id: int) -> Dict:
    """Fetch anime metadata from AniList"""
    cache_key = f"anilist:meta:{anilist_id}"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data
    
    url = "https://graphql.anilist.co"
    query = """
    query ($id: Int) {
      Media (id: $id, type: ANIME) {
        id
        status
        episodes
        bannerImage
        coverImage {
          large
          extraLarge
        }
        title {
          romaji
          english
          native
        }
        synonyms
        description
        startDate {
          year
          month
          day
        }
        endDate {
          year
          month
          day
        }
        streamingEpisodes {
          title
          thumbnail
          url
        }
        nextAiringEpisode {
          episode
          airingAt
        }
      }
    }
    """
    
    try:
        response = await client.post(
            url, 
            json={"query": query, "variables": {"id": anilist_id}},
            timeout=Config.REQUEST_TIMEOUT
        )
        
        if response.status_code != 200:
            logger.error(f"AniList API error: Status {response.status_code}")
            return {}
        
        data = response.json()
        media = data.get("data", {}).get("Media", {})
        if not media: return {}
        
        # Format streaming episodes
        raw_episodes = media.get("streamingEpisodes", [])
        formatted_episodes = []
        
        for index, ep in enumerate(raw_episodes, start=1):
            formatted_episodes.append({
                "episode_number": index,
                "title": ep.get("title", f"Episode {index}"),
                "thumbnail": ep.get("thumbnail"),
                "url": ep.get("url")
            })
        
        # Fallback to generated episode list if no streaming episodes
        if not formatted_episodes and media.get("episodes"):
            total_episodes = media.get("episodes")
            for i in range(1, total_episodes + 1):
                formatted_episodes.append({
                    "episode_number": i,
                    "title": f"Episode {i}",
                    "thumbnail": None,
                    "url": None
                })
        
        result = {
            "anilist_id": media.get("id"),
            "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
            "title_romaji": media.get("title", {}).get("romaji"),
            "title_english": media.get("title", {}).get("english"),
            "title_native": media.get("title", {}).get("native"),
            "synonyms": media.get("synonyms") or [],
            "total_episodes": media.get("episodes"),
            "status": media.get("status"),
            "banner": media.get("bannerImage"),
            "cover": media.get("coverImage", {}).get("extraLarge") or media.get("coverImage", {}).get("large"),
            "description": media.get("description"),
            "start_date": media.get("startDate"),
            "end_date": media.get("endDate"),
            "next_airing_episode": media.get("nextAiringEpisode"),
            "episodes_list": formatted_episodes
        }
        
        # Cache the result
        if result:
            await set_cached_response(cache_key, result, ttl_seconds=Config.CACHE_TTL_SECONDS)
        
        return result
        
    except Exception as e:
        logger.error(f"Error fetching from AniList: {e}")
        return {}

async def fetch_tmdb_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for anime titles"""
    cache_key = f"tmdb:search:{query.lower()}"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])
    
    url = "https://api.themoviedb.org/3/search/tv"
    data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})
    
    if not data:
        return []

    items = data.get("results", [])[:limit]
    # One batched lookup for every candidate's anilist mapping instead of a query
    # per result.
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])

    results = []
    for item in items:
        tmdb_id = item.get("id")
        anilist_id = anilist_by_tmdb.get(tmdb_id) if tmdb_id else None
        if anilist_id:
            results.append({
                "title": item.get("name") or item.get("original_name"),
                "tmdb_id": tmdb_id,
                "anilist_id": anilist_id,
                "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
                "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
                "vote_average": item.get("vote_average")
            })

    # Cache search results for 24 hours
    await set_cached_response(cache_key, {"results": results}, ttl_seconds=Config.CACHE_TTL_SECONDS)
    return results

async def fetch_trending_anime(client: httpx.AsyncClient, limit: int = 12) -> List[Dict]:
    """Fetch trending anime from TMDB"""
    cache_key = "tmdb:trending"

    # L1: in-process cache (no DB round-trip on a hit).
    local = _local_get(cache_key)
    if local is not None:
        return local

    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        results = cached_data.get("results", [])
        _local_set(cache_key, results)
        return results

    url = "https://api.themoviedb.org/3/discover/tv"
    params = {
        "page": 1,
        "include_adult": "false",
        "language": "en-US",
        "with_genres": "16",  # Animation genre
        "with_original_language": "ja",  # Japanese originals
        "sort_by": "popularity.desc",
        "vote_count.gte": 100  # Minimum votes for quality filter
    }
    
    data = await fetch_with_retry(client, url, params=params)
    
    if not data:
        return []

    items = data.get("results", [])[:limit]
    # One batched lookup for every candidate's anilist mapping instead of a query
    # per result.
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])

    trending_list = []
    for item in items:
        tmdb_id = item.get("id")
        anilist_id = anilist_by_tmdb.get(tmdb_id) if tmdb_id else None
        if anilist_id:
            trending_list.append({
                "title": item.get("name") or item.get("original_name"),
                "tmdb_id": tmdb_id,
                "anilist_id": anilist_id,
                "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
                "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
                "vote_average": item.get("vote_average")
            })

    # Cache trending results (DB for cross-replica reuse + L1 for this process).
    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
    _local_set(cache_key, trending_list)
    return trending_list

# --- NON-ANIME TV SHOWS (secondary, additive) -------------------------------
# These mirror the anime discovery helpers above but invert the AniList gate:
# they surface TMDB TV results that are NOT mapped anime, so the site can also
# play general (non-anime) series through the same TMDB-keyed pipeline (/info,
# /watch/{tmdb_id}/{season}/{episode}) and the s.to scraper, which matches by
# title. Anime stays priority 1 — these are a separate, parallel surface and the
# anime helpers/endpoints above are left completely untouched.

def _looks_like_anime(item: Dict) -> bool:
    """Heuristic: a TMDB TV item that is Japanese Animation. Used to keep anime
    (including titles we haven't mapped in Fribb yet) out of the *shows* surface,
    so the two stay cleanly separated even at the edges."""
    genres = item.get("genre_ids") or []
    return 16 in genres and item.get("original_language") == "ja"


def _show_item(item: Dict) -> Dict:
    """Shape one TMDB TV search/discover result as a non-anime show entry. Keyed
    by tmdb_id (no anilist_id) and tagged ``kind: "show"`` so the frontend routes
    it through the TMDB-keyed show pages instead of the AniList ones."""
    return {
        "title": item.get("name") or item.get("original_name"),
        "tmdb_id": item.get("id"),
        "anilist_id": None,
        "kind": "show",
        "poster": _tmdb_img(item.get("poster_path")) if item.get("poster_path") else None,
        "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
        "vote_average": item.get("vote_average"),
    }


async def fetch_tmdb_show_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for general TV shows, excluding anime.

    Excludes (a) titles that already map to an AniList entry — those are anime,
    served by /search/anime — and (b) anything that looks like Japanese animation,
    so unmapped anime doesn't leak into the shows surface."""
    cache_key = f"tmdb:search_shows:{query.lower()}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])

    url = "https://api.themoviedb.org/3/search/tv"
    data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})
    if not data:
        return []

    items = data.get("results", [])
    # One batched lookup so we can drop anything already mapped as anime.
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])

    results: List[Dict] = []
    for item in items:
        tmdb_id = item.get("id")
        if not tmdb_id or anilist_by_tmdb.get(tmdb_id) or _looks_like_anime(item):
            continue
        if not item.get("poster_path"):
            continue  # posterless rows are usually junk/duplicates — skip for a clean grid
        results.append(_show_item(item))
        if len(results) >= limit:
            break

    await set_cached_response(cache_key, {"results": results}, ttl_seconds=Config.CACHE_TTL_SECONDS)
    return results


async def fetch_trending_shows(client: httpx.AsyncClient, limit: int = 10) -> List[Dict]:
    """Fetch trending non-anime TV shows from TMDB (popular, excluding animation)."""
    cache_key = "tmdb:trending_shows"

    local = _local_get(cache_key)
    if local is not None:
        return local

    cached_data = await get_cached_response(cache_key)
    if cached_data:
        results = cached_data.get("results", [])
        _local_set(cache_key, results)
        return results

    url = "https://api.themoviedb.org/3/discover/tv"
    params = {
        "page": 1,
        "include_adult": "false",
        "language": "en-US",
        "without_genres": "16",          # exclude Animation (keeps anime out)
        "sort_by": "popularity.desc",
        "vote_count.gte": 200,           # quality floor
    }
    data = await fetch_with_retry(client, url, params=params)
    if not data:
        return []

    items = data.get("results", [])
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])

    trending_list: List[Dict] = []
    for item in items:
        tmdb_id = item.get("id")
        if not tmdb_id or anilist_by_tmdb.get(tmdb_id) or _looks_like_anime(item):
            continue
        if not item.get("poster_path"):
            continue
        trending_list.append(_show_item(item))
        if len(trending_list) >= limit:
            break

    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
    _local_set(cache_key, trending_list)
    return trending_list

# --- SCRAPER HELPER FUNCTIONS ---
async def fetch_tmdb_localized_titles(client: httpx.AsyncClient, tmdb_id: int) -> List[str]:
    """German-language titles for a TMDB show, for the German scraper sites.

    The German streaming sources (s.to, aniworld) list many shows under their
    *German broadcast title*, not the English one TMDB hands us first — e.g. NCIS
    is "Navy CIS" on s.to, so plain title matching misses the show entirely. We
    pull the German title(s) from TMDB so the title-based scrapers get them as
    extra search candidates: the localized name from ``/translations`` (de) plus
    any DE/AT/CH entries in ``/alternative_titles``. Cached (these are stable);
    returns an empty list on failure (matching just falls back to the English
    title, i.e. today's behaviour)."""
    cache_key = f"tmdb:detitles:{tmdb_id}"
    cached = _local_get(cache_key)
    if cached is not None:
        return cached

    titles: List[str] = []
    seen: set = set()

    def _add(value: Optional[str]) -> None:
        value = (value or "").strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            titles.append(value)

    translations, alternatives = await asyncio.gather(
        fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}/translations"),
        fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}/alternative_titles"),
    )

    for t in ((translations or {}).get("translations") or []):
        if t.get("iso_639_1") == "de":
            _add((t.get("data") or {}).get("name"))
    for a in ((alternatives or {}).get("results") or []):
        if a.get("iso_3166_1") in ("DE", "AT", "CH"):
            _add(a.get("title"))

    _local_set(cache_key, titles, ttl=86400)
    return titles


async def run_single_scraper(scraper_class, tmdb_id: int, season_num: int, episode_num: int, anilist_data: Dict) -> List:
    """Run one scraper through the unified search -> embeds pipeline."""
    scraper = scraper_class()
    try:
        media_ctx = {
            "tmdb_id": tmdb_id,
            "tmdb_season": season_num,
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
    the absolute iframe URLs we emit for the ad-free proxy sources (Movish,
    PlayIMDb, AnimeSuge) get blocked as mixed content on the HTTPS frontend. Trust
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
    used to turn a resolver's relative proxy/player path (Movish, PlayIMDb,
    AnimeSuge, Jellyfin) into an absolute iframe src the frontend can load.

    ``language`` is an optional human-readable audio/subtitle label (e.g.
    "German Dub") for these embeds, known by some scrapers (aniworld) and not
    others. When set, it is stamped onto every resolved stream so the frontend
    can show it; otherwise the streams carry no language and it stays blank.
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
                # A resolver may return a bare URL string (the common case) or a
                # dict {"url", "subtitles"} when it also has external subtitle
                # tracks (ShowBox/Febbox). Normalise to (url, subtitles).
                subtitles = None
                if isinstance(resolved, dict):
                    subtitles = resolved.get("subtitles") or None
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

                    if "_proxy/h/" in direct_video_url or direct_video_url.startswith("/player"):
                        # Backend-hosted player page (Movish player-proxy, or our
                        # /player wrapping a Jellyfin/PlayIMDb/AnimeSuge stream):
                        # the frontend just iframes it.
                        resolved_streams.append({
                            "source": matched_resolver.source_name,
                            "type": "iframe",
                            "url": abs_url
                        })
                    else:
                        stream_type = "hls" if "m3u8" in direct_video_url.lower() else "mp4"
                        stream_obj = {
                            "source": matched_resolver.source_name,
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
    # L1: in-process cache for the whole item list (no DB round-trip on a hit).
    items = _local_get(cache_key)
    if items is None:
        cached = await get_cached_response(cache_key)
        if cached and "items" in cached:
            items = cached["items"]
        else:
            loop = asyncio.get_event_loop()
            items = await loop.run_in_executor(None, get_catalogue_items)
            if items:
                await set_cached_response(cache_key, {"items": items}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
        if items:
            _local_set(cache_key, items)

    # Format + genre breakdowns over the FULL catalogue (before any filtering).
    counts: Dict[str, int] = {}
    genre_counts: Dict[str, int] = {}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
        for g in it.get("genres") or []:
            genre_counts[g] = genre_counts.get(g, 0) + 1
    categories = [{"category": k, "count": v} for k, v in sorted(counts.items())]
    genres = [{"genre": k, "count": v} for k, v in sorted(genre_counts.items())]

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
        "categories": categories,
        "genres": genres,
        "animes": animes,
    })

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
                                base_url: str = ""):
    """Progressively scrape + resolve an episode, yielding NDJSON lines as each
    source is found — instead of waiting for every scraper to finish.

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

    yield _ndjson({
        "type": "meta",
        "success": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "episode_number": episode_number,
        "anilist_id": anilist_id,
        "title": title,
    })

    # German streaming scrapers (s.to, aniworld) list many non-anime shows under
    # their German broadcast title — e.g. NCIS is "Navy CIS" on s.to — which TMDB
    # only exposes via /translations, so English-title matching alone misses them.
    # Feed the German title(s) in as extra search candidates. Only on the
    # no-AniList path: AniList-mapped anime already carry their own synonyms and
    # that matching stays byte-identical.
    if not anilist_id:
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
                scraper_class, tmdb_id, season_number, episode_number, media_ctx
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
            yield _ndjson({
                "type": "stream",
                "source": stream["source"],
                "streamType": stream["type"],
                "url": stream["url"],
                "language": stream.get("language"),
                "subtitles": stream.get("subtitles"),
            })
        yield _ndjson({"type": "done", "count": count})
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

# --- MOVISH AD-FREE PROXY ("movish" source) ---
@app.api_route("/movish_proxy/h/{host}/{path:path}", methods=["GET", "POST"])
async def movish_proxy(request: Request, host: str, path: str):
    """Same-origin reverse proxy for the Movish player. Downloads the page/asset,
    sandboxes any embed-provider iframe + neutralises pop-ups, rewrites
    sub-resource URLs back through this proxy, and serves it.

    Text resources (HTML/JS/CSS, and the CORS-less /embed/api JSON) are buffered,
    cleaned and rewritten; /v1/play media is streamed straight through with Range
    passthrough so seeking works and large files aren't held in memory. Scoped to
    the api.movish.net host allow-list in resolvers.movish (rejects anything else
    to avoid an open proxy)."""
    body = await request.body() if request.method == "POST" else None
    try:
        status, content_type, headers, payload = await movish_proxy_fetch(
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
        logger.error(f"Movish proxy upstream error for {host}/{path}: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    # Streaming media (video/binary) — forward Range/length headers.
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- PLAYIMDB AD-FREE HLS PROXY ("playimdb" source) ---
@app.get("/playimdb_proxy")
async def playimdb_proxy(request: Request):
    """Signed, same-origin HLS proxy for the PlayIMDb source. Fetches a signed
    upstream playlist/segment with the Referer the PlayIMDb CDNs require
    (injected server-side), rewrites playlists so sub-resources flow back
    through this proxy, and streams segments through with Range passthrough.

    The upstream CDN host rotates per request, so instead of a host allow-list
    this proxy verifies an HMAC on the ``u`` URL (see resolvers.playimdb) and
    refuses anything unsigned — closing the open-proxy / SSRF hole. No PlayIMDb
    player or ad code is ever involved; the resolver extracts the raw stream and
    wraps it in /player."""
    url = request.query_params.get("u")
    sig = request.query_params.get("s")
    try:
        status, content_type, headers, payload = await playimdb_proxy_fetch(
            url=url,
            sig=sig,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"PlayIMDb proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- VOE STREAM PROXY ("Voe" source) ---
@app.get("/voe_proxy")
async def voe_proxy(request: Request):
    """Signed, same-origin HLS proxy for the VOE source. VOE's delivery CDN binds
    its stream token to the IP/ASN that resolved the embed (note the ``asn=``
    query param), so the raw playlist/segment URLs 403 from the viewer's browser
    even though they play for the backend. This fetches the signed upstream
    playlist/segment server-side, rewrites playlists so sub-resources flow back
    through this proxy, and streams segments through with Range passthrough.

    The CDN host rotates, so instead of a host allow-list this proxy verifies an
    HMAC on the ``u`` URL (see resolvers.voe) and refuses anything unsigned —
    closing the open-proxy / SSRF hole."""
    url = request.query_params.get("u")
    sig = request.query_params.get("s")
    try:
        status, content_type, headers, payload = await voe_proxy_fetch(
            url=url,
            sig=sig,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"VOE proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- VIDMOLY STREAM PROXY ("Vidmoly" source) ---
@app.get("/vidmoly_proxy")
async def vidmoly_proxy(request: Request):
    """Signed, same-origin HLS proxy for the Vidmoly source. Fetches the signed
    upstream playlist/segment server-side (with the vidmoly Referer), rewrites
    playlists so sub-resources flow back through this proxy, and streams segments
    through with Range passthrough.

    This exists so the Vidmoly stream can be played in the same-origin Crimson
    ``/player`` (which gives a real, fullscreen-capable player) instead of being
    handed to the frontend as a bare cross-origin URL. The CDN host rotates, so
    instead of a host allow-list the proxy verifies an HMAC on the ``u`` URL (see
    resolvers.vidmoly) and refuses anything unsigned — closing the open-proxy /
    SSRF hole."""
    url = request.query_params.get("u")
    sig = request.query_params.get("s")
    try:
        status, content_type, headers, payload = await vidmoly_proxy_fetch(
            url=url,
            sig=sig,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Vidmoly proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- VIDSRC STREAM PROXY ("VidSrc" / aniwatch megaplay source) ---
@app.get("/vidsrc_proxy")
async def vidsrc_proxy(request: Request):
    """Signed, same-origin HLS proxy for aniwatch's "VidSrc" (megaplay) source.
    The megaplay delivery CDN is Referer-gated (403s without
    ``Referer: https://megaplay.buzz/``), which the viewer's browser can't set on
    its media fetches, so the playlist/segments are fetched server-side here (with
    that Referer), playlists rewritten so sub-resources flow back through this
    proxy, and segments streamed through with Range passthrough.

    The CDN host rotates, so instead of a host allow-list this proxy verifies an
    HMAC on the ``u`` URL (see resolvers.vidsrc) and refuses anything unsigned —
    closing the open-proxy / SSRF hole."""
    url = request.query_params.get("u")
    sig = request.query_params.get("s")
    try:
        status, content_type, headers, payload = await vidsrc_proxy_fetch(
            url=url,
            sig=sig,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"VidSrc proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- CINEMA.BZ STREAM PROXY ("Cinema.bz (…)" sources) ---
@app.get("/cinemabz_proxy")
async def cinemabz_proxy(request: Request):
    """Signed, same-origin HLS proxy for the cinema.bz sources. cinema.bz's
    upstream HLS CDN is Referer-gated (403s without ``Referer: https://cinema.bz/``)
    and the 1shows CDN serves CORS scoped to cinema.bz's own origin (not ``*``), so
    the raw playlist/segments can't be fetched by the viewer's browser. This fetches
    the signed upstream playlist/segment server-side (with the cinema.bz Referer),
    rewrites playlists so sub-resources flow back through this proxy, and streams
    segments through with Range passthrough.

    The upstream CDN host rotates, so instead of a host allow-list this proxy
    verifies an HMAC on the ``u`` URL (see resolvers.cinemabz) and refuses anything
    unsigned — closing the open-proxy / SSRF hole."""
    url = request.query_params.get("u")
    sig = request.query_params.get("s")
    try:
        status, content_type, headers, payload = await cinemabz_proxy_fetch(
            url=url,
            sig=sig,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Cinema.bz proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- SHOWBOX/FEBBOX DIRECT-FILE PROXY ("showbox" source) ---
@app.get("/febbox_proxy")
async def febbox_proxy(request: Request):
    """Signed, same-origin proxy for the ShowBox/Febbox source. Febbox's player
    hands back direct mp4 links on a rotating OSS CDN; proxying keeps playback
    same-origin (no CORS surprises), survives host rotation and gives Range
    passthrough for seeking. Some qualities come back as HLS, which is rewritten
    so sub-resources flow back through here.

    The OSS host rotates, so instead of a host allow-list this proxy verifies an
    HMAC on the ``u`` URL (see resolvers.febbox) and refuses anything unsigned —
    closing the open-proxy / SSRF hole. The febbox ``ui`` token never reaches the
    browser: it's only used server-side to mint the direct link in the resolver."""
    url = request.query_params.get("u")
    sig = request.query_params.get("s")
    try:
        status, content_type, headers, payload = await febbox_proxy_fetch(
            url=url,
            sig=sig,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Febbox proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- ANIMESUGE AD-FREE STREAM PROXY ("animesuge" source) ---
@app.get("/animesuge_proxy")
async def animesuge_proxy(request: Request):
    """Signed, same-origin proxy for the AnimeSuge source. Fetches a signed
    upstream direct file (mp4/m3u8) server-side, rewrites HLS playlists so
    sub-resources flow back through this proxy, and streams media through with
    Range passthrough.

    The direct-file CDN host can rotate, so instead of a host allow-list this
    proxy verifies an HMAC on the ``u`` URL (see resolvers.animesuge) and refuses
    anything unsigned — closing the open-proxy / SSRF hole. No AnimeSuge or
    third-party player/ad code is ever involved; the scraper extracts the raw
    direct file and the resolver wraps it in /player."""
    url = request.query_params.get("u")
    sig = request.query_params.get("s")
    try:
        status, content_type, headers, payload = await animesuge_proxy_fetch(
            url=url,
            sig=sig,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"AnimeSuge proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


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
        status, content_type, headers, payload = await jellyfin_proxy_fetch(
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

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type, headers=headers)
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


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
    if not show:
        raise HTTPException(status_code=404, detail="Show not found on TMDB")

    anime_info = anime_info or {}
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
        "seasons": seasons_data,
        "extras": get_show_extras(tmdb_id),
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
    if not show:
        raise HTTPException(status_code=404, detail="Show not found on TMDB")

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
        # General shows carry no AniList specials/OVAs/movies mapping.
        "extras": [],
    }

# --- HEALTH CHECK ENDPOINT ---
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
    """Custom HTTP exception handler"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
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
            "detail": str(exc) if os.getenv("DEBUG") else None
        }
    )