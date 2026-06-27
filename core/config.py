"""
Central application configuration.

Holds the ``Config`` class (all env-driven knobs) and the TMDB auth headers,
moved out of ``api.py`` so every module — the app, the shared HTTP client, the
metadata fetchers — imports its settings from one place instead of reaching back
into ``api.py`` (which would be a circular import).

``load_dotenv()`` runs at import time, before ``Config`` reads the environment,
because ``Config``'s class body calls ``os.getenv`` as it's defined. It's
idempotent, so ``api.py`` calling it again is harmless.
"""

import os

from dotenv import load_dotenv

# Load environment variables before Config reads them below.
load_dotenv()


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

    # Only the dedicated cache-worker service runs the background ffmpeg download
    # loop; the api/api-sync replicas just mint cache tickets and claim pending
    # rows (the DB row is the job queue, so a download survives an api redeploy).
    # Defaults true so a single-container (docker-compose) deploy still caches
    # without extra config; the Swarm stack sets it false on api/api-sync and true
    # on cache-worker. The DB claim dedupes if more than one process runs it.
    RUN_CACHE_WORKER = os.getenv("RUN_CACHE_WORKER", "true").lower() not in ("0", "false", "no")

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
    "accept": "application/json",
}
