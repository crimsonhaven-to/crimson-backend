"""The public endpoints: the root, Lumi's shrine, pre-login config and health.
All four are exempt from the login wall."""

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core import lumi, migrations
from core.config import Settings, get_settings
from core.db_pool import get_connection
from core.version import VERSION
from local_engine.fs import is_configured as local_is_configured
from metadata_engine import sync_status
from resolvers import ALL_RESOLVERS
from resolvers.jellyfin import is_configured as jellyfin_is_configured
from scrapers import ALL_SCRAPERS

logger = logging.getLogger("crimson.system")

router = APIRouter(tags=["system"])


@router.get("/")
async def root():
    return {
        "version": VERSION,
        "message": "Hehe, you found me, Luminas Crimsonveil, the eternal empress of this realm. Be proud, little mortal. ✨",
    }


@router.get("/lumi")
async def lumi_blessing():
    """The shrine behind the client's Konami-code page."""
    return {"empress": lumi.EMPRESS, "title": lumi.TITLE, "blessing": lumi.blessing(), "sigil": "🦇"}


@router.get("/config")
async def public_config(settings: Settings = Depends(get_settings)):
    """Feature flags the client needs before login, such as ``demo_mode``, which
    drops the invite-code field. Reachable without a session, so booleans only."""
    return {
        "demo_mode": settings.demo_mode,
        "require_login": settings.require_login,
        "manga_enabled": settings.manga_enabled,
        "live_tv_enabled": settings.iptv_enabled,
        "local_library_enabled": local_is_configured(),
    }


def _entries_count() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM anime_entries").fetchone()["n"]


@router.get("/health")
async def health_check(settings: Settings = Depends(get_settings)):
    """The Swarm healthcheck, every 30s per replica, so the count runs off the loop."""
    try:
        count = await asyncio.to_thread(_entries_count)
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                # Unauthenticated, so detail only under DEBUG.
                "error": str(e) if settings.debug else "database unavailable",
            },
        )
    return {
        "status": "healthy",
        "database": "connected",
        "entries_count": count,
        # A cold boot reports the background sync as "running" while healthy.
        "mapping_sync": sync_status.snapshot(),
        # The startup snapshot, so a version-skewed replica is visible to an
        # uptime check without a query per probe.
        "schema": migrations.cached_status(),
        "scrapers_available": len(ALL_SCRAPERS),
        "resolvers_available": len(ALL_RESOLVERS),
        "jellyfin_configured": jellyfin_is_configured(),
        "local_sources_configured": local_is_configured(),
    }
