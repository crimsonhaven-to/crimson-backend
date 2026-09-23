"""Unauthenticated system endpoints: root greeting, Lumi's shrine, config, health.

All four are whitelisted on the login wall; see api.py's ``_PUBLIC_EXACT``.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.config import Settings, get_settings
from core.version import VERSION
from core import lumi
from core import migrations
from resolvers import ALL_RESOLVERS
from resolvers.jellyfin import is_configured as jellyfin_is_configured
from scrapers import ALL_SCRAPERS
from local_engine.fs import is_configured as local_is_configured
from metadata_engine import sync_status

from web.context import get_db_connection

logger = logging.getLogger("crimson.system")

router = APIRouter()


@router.get("/")
async def root():
    """API root."""
    return {
        "version": VERSION,
        "message": "Hehe, you found me, Luminas Crimsonveil, the eternal empress of this realm. Be proud, little mortal. ✨",
    }


@router.get("/lumi")
async def lumi_blessing():
    """A little shrine to the empress, behind the frontend's Konami-code page.
    Public, so Lumi greets even the uninvited."""
    return {
        "empress": lumi.EMPRESS,
        "title": lumi.TITLE,
        "blessing": lumi.blessing(),
        "sigil": "🦇",
    }


@router.get("/config")
async def public_config(settings: Settings = Depends(get_settings)):
    """Feature flags the frontend needs before login, such as ``demo_mode``, which
    drops the invite-code field. Reachable without a session, so booleans only."""
    return {
        "demo_mode": settings.demo_mode,
        "require_login": settings.require_login,
        "manga_enabled": settings.manga_enabled,
        "live_tv_enabled": settings.iptv_enabled,
        "local_library_enabled": local_is_configured(),
    }


def _entries_count() -> int:
    """The row count behind /health's ``entries_count``.

    A plain sync function so the route can hand it to a worker thread. It is a
    real Postgres round-trip and the healthcheck fires it every 30s per replica,
    so running it inline would stall any /watch stream sharing that worker."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM anime_entries")
        return cursor.fetchone()["n"]


@router.get("/health")
async def health_check():
    """Liveness and readiness for the Swarm healthcheck."""
    try:
        count = await run_in_threadpool(_entries_count)

        return {
            "status": "healthy",
            "database": "connected",
            "entries_count": count,
            # The initial sync runs in the background, so a cold boot reports
            # "running" while /health already answers healthy, then settles.
            "mapping_sync": sync_status.snapshot(),
            # From the startup snapshot, so no DB hit on a probe this frequent.
            # Makes a version-skewed replica visible to an uptime check rather
            # than only as a request-time error.
            "schema": migrations.cached_status(),
            "scrapers_available": len(ALL_SCRAPERS),
            "resolvers_available": len(ALL_RESOLVERS),
            "jellyfin_configured": jellyfin_is_configured(),
            "local_sources_configured": local_is_configured()
        }
    except Exception as e:
        # Logged in full server-side, but an unauthenticated probe sees detail
        # only under DEBUG.
        logger.error(f"Health check failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e) if get_settings().debug else "database unavailable",
            },
        )
