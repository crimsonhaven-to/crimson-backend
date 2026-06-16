"""
Changelog API — a public, read-only view of the project's GitHub Releases.

``GET /changelog`` returns the cached release notes (newest first). It's public
(listed in api.py's login-wall allowlist) so a landing/about page can show it
without a session. The heavy lifting — fetching + caching from GitHub — lives in
``changelog_engine.service`` (see there for configuration).
"""

import os

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .service import ChangelogService, DEFAULT_REPO

router = APIRouter(tags=["changelog"])
service = ChangelogService()


@router.get("/changelog")
async def get_changelog():
    """Release notes for the haven, newest first.

    Returns 503 until a ``GITHUB_TOKEN`` is configured (see the service module).
    Each entry carries ``{tag, name, body (Markdown), published_at, url,
    prerelease, author}``. ``stale: true`` means GitHub was unreachable on the last
    refresh and these are the last-known notes (served rather than failing).
    """
    if not service.configured():
        raise HTTPException(status_code=503, detail="Changelog is not configured")
    data = await run_in_threadpool(service.get)
    return {
        "success": True,
        "repo": (os.getenv("GITHUB_REPO") or DEFAULT_REPO).strip(),
        "count": len(data["entries"]),
        "stale": data["stale"],
        "changelog": data["entries"],
    }
