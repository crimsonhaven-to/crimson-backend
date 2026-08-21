"""
Central application configuration: every env-driven knob plus the TMDB auth
headers. Lives here rather than in api.py so any module can import its settings
without a circular import back through the app.

``load_dotenv()`` runs at import time because ``Config``'s class body calls
``os.getenv`` as it is defined. It is idempotent, so api.py calling it again is
harmless.
"""

import os

from dotenv import load_dotenv

# Must run before Config's class body reads the environment below.
load_dotenv()


class Config:
    TMDB_API_KEY = os.getenv("TMDB_API_KEY")
    # Mapping and accounts live in PostgreSQL, configured via DATABASE_URL /
    # POSTGRES_* and pooled in db_pool.
    CACHE_TTL_SECONDS = 86400  # 24 hours
    TRENDING_CACHE_TTL_SECONDS = 21600  # 6 hours
    MAX_CONCURRENT_REQUESTS = 10
    REQUEST_TIMEOUT = 30.0
    MAX_RETRIES = 3
    RETRY_BACKOFF_FACTOR = 1.0

    # The sync rebuilds the mapping tables wholesale, so keep this on exactly one
    # replica. See README, "Deploying to Docker Swarm".
    RUN_DB_SYNC = os.getenv("RUN_DB_SYNC", "true").lower() not in ("0", "false", "no")

    # --- Non-anime metadata maintenance (tmdb_shows / tmdb_movies) ----------
    # These tables are written lazily on open and from search. All the heavy work
    # below is pinned to the single RUN_DB_SYNC replica, so exactly one container
    # ever churns this much metadata.
    #
    # Nothing upstream tells us when a TMDB row changed, unlike the Fribb dataset,
    # so the catalogue is swept in slices: each night the oldest
    # 1/METADATA_REFRESH_BUCKETS of each table is re-pulled, refreshing everything
    # over a full cycle. Freshly opened rows sort last and age to the front.
    METADATA_REFRESH_BUCKETS = int(os.getenv("METADATA_REFRESH_BUCKETS", "14"))
    METADATA_REFRESH_HOUR = int(os.getenv("METADATA_REFRESH_HOUR", "4"))  # 0-23, server local time
    #
    # Catalogue backfill pages TMDB discover to pre-populate the tables beyond
    # what has been browsed. Off by default, since demand-driven fill suffices for
    # most installs. Can also be queued from the Admin dashboard. Paced between
    # pages to stay rate-limit and replication friendly.
    RUN_METADATA_BACKFILL = os.getenv("RUN_METADATA_BACKFILL", "false").lower() in ("1", "true", "yes")
    METADATA_BACKFILL_PAGES = int(os.getenv("METADATA_BACKFILL_PAGES", "100"))

    # Only the cache-worker runs the ffmpeg download loop; api replicas just mint
    # tickets and claim rows. The DB row is the queue, so a download survives an
    # api redeploy, and the claim dedupes if more than one process runs it.
    # Defaults true so a single-container deploy still caches without extra config.
    RUN_CACHE_WORKER = os.getenv("RUN_CACHE_WORKER", "true").lower() not in ("0", "false", "no")

    # Same rationale as RUN_CACHE_WORKER: only the download-worker runs the aria2
    # poll loop, while api replicas write pending rows and issue pause/resume
    # straight to the sidecar.
    RUN_DOWNLOAD_WORKER = os.getenv("RUN_DOWNLOAD_WORKER", "true").lower() not in ("0", "false", "no")

    # Seeds the first admin so /admin is reachable without hand-editing the DB;
    # after that admins promote each other from the dashboard. Only applies to
    # accounts that already exist, and never creates one.
    ADMIN_EMAILS = [
        e.strip() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
    ]

    # When true every content endpoint requires a session bearer token. A few
    # paths stay public: auth, health, the Ko-fi webhook, and the signed stream
    # proxies that media elements load without headers.
    REQUIRE_LOGIN = os.getenv("REQUIRE_LOGIN", "true").lower() not in ("0", "false", "no")

    # Bypasses the signup invite gate and wipes all non-admin account data
    # nightly, so an open-signup demo can't grow without bound. Admins survive the
    # reset. A demo runs with no sources configured, so the only growth is text
    # rows, which the nightly reset caps.
    DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes", "on")
    # Hour the nightly reset runs at, in server time (UTC in the container).
    DEMO_RESET_HOUR = int(os.getenv("DEMO_RESET_HOUR", "4"))

    # --- Lumi, the chatbot (see chat_engine) -------------------------------
    # Only the provider API keys live here. Everything else about the feature is
    # operator state in chat_settings, managed from the dashboard, so changing it
    # needs no redeploy.
    #
    # Keys stay out of the database so a dump of it never carries billable
    # credentials; the dashboard is told only whether a key is present. Set
    # whichever provider you intend to use.
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or None
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None

    # Overridable via ALLOWED_ORIGINS so a deploy can lock these down without a
    # code change.
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

TMDB_HEADERS = {
    "Authorization": f"Bearer {Config.TMDB_API_KEY}",
    "accept": "application/json",
}
