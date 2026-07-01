"""Unauthenticated system endpoints: root greeting, Lumi's shrine, config, health.

All four are whitelisted on the login wall (see ``api.py``'s ``_PUBLIC_EXACT``).
Lifted verbatim from ``api.py``.
"""

import logging
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.config import Config
from core.version import VERSION
from core import lumi
from resolvers import ALL_RESOLVERS
from resolvers.jellyfin import is_configured as jellyfin_is_configured
from scrapers import ALL_SCRAPERS
from local_engine.fs import is_configured as local_is_configured

from web.context import get_db_connection

logger = logging.getLogger("crimson.system")

router = APIRouter()


@router.get("/")
async def root():
    """API root endpoint"""
    return {
        "version": VERSION,
        "message": "Hehe, you found me, Luminas Crimsonveil, the eternal empress of this realm. Be proud, little mortal. ✨",
    }


@router.get("/lumi")
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


@router.get("/config")
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


@router.get("/health")
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
