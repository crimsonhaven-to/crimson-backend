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

    # --- Non-anime metadata maintenance (tmdb_shows / tmdb_movies) ----------
    # The non-anime show/movie tables are written lazily (on open + from search/
    # trending). These knobs keep them fresh and, optionally, pre-seed them.
    # ALL of this heavy work is pinned to the single RUN_DB_SYNC replica (the
    # api-sync container that already owns the Fribb resync) so exactly one
    # container ever churns this much metadata — the serving replicas never do.
    #
    # Nightly staleness refresh: there is no upstream (unlike the Fribb dataset) to
    # tell us when a TMDB row changed, so the catalogue is swept in slices. Every
    # night at METADATA_REFRESH_HOUR the oldest 1/METADATA_REFRESH_BUCKETS of each
    # table is re-pulled from TMDB; over a full cycle (default 14 nights) every row
    # is refreshed, then it repeats. Freshly-opened rows sort last, so they're
    # naturally skipped until they age to the front again.
    METADATA_REFRESH_BUCKETS = int(os.getenv("METADATA_REFRESH_BUCKETS", "14"))
    METADATA_REFRESH_HOUR = int(os.getenv("METADATA_REFRESH_HOUR", "4"))  # 0-23, server local time
    #
    # Catalogue backfill: page TMDB discover to pre-populate the tables beyond what's
    # been browsed. Off by default (demand-driven fill is enough for most installs).
    # Can be kicked off from the Admin dashboard at any time — the request is queued
    # in the DB and drained by api-sync — or run once at startup via the flag below.
    # Paced between pages to stay rate-limit + WAL/replication friendly.
    RUN_METADATA_BACKFILL = os.getenv("RUN_METADATA_BACKFILL", "false").lower() in ("1", "true", "yes")
    METADATA_BACKFILL_PAGES = int(os.getenv("METADATA_BACKFILL_PAGES", "100"))

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

    # Demo deployment switch (e.g. demo.crimsonhaven.to). When true: the signup
    # invite gate is bypassed so anyone can register, and all non-admin account data
    # (accounts, sessions, favorites, watch progress, invites, challenges) is wiped
    # nightly so an open-signup demo can't grow without bound. Admin accounts (seeded
    # from ADMIN_EMAILS) survive the reset. Off by default — a normal deploy is
    # unaffected. A demo is expected to run with NO sources configured (nothing
    # resolves), so the only growth is text rows, capped by the nightly reset.
    DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes", "on")
    # Hour (server time, UTC in the container) the nightly DEMO_MODE reset runs at.
    DEMO_RESET_HOUR = int(os.getenv("DEMO_RESET_HOUR", "4"))

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
