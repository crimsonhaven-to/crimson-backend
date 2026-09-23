"""The public /changelog, so a landing page can show release notes without a
session."""

import asyncio

from fastapi import APIRouter, HTTPException

from core.config import get_settings

from .service import service

router = APIRouter(tags=["changelog"])


@router.get("/changelog")
async def get_changelog():
    """Newest first; 503 until GITHUB_TOKEN is set. ``stale`` means GitHub was
    unreachable on the last refresh and these are the last known notes."""
    if not service.configured():
        raise HTTPException(status_code=503, detail="Changelog is not configured")
    data = await asyncio.to_thread(service.get)
    return {
        "success": True,
        "repo": get_settings().github_repo,
        "count": len(data["entries"]),
        "stale": data["stale"],
        "changelog": data["entries"],
    }
