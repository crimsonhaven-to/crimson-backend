"""Recommendations from the genres already in the database: a personalized feed
and "more like this". No external calls."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from account_engine.deps import require_user
from core.rate_limit import limiter

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["recommendations"])


@router.get("/recommendations")
@limiter.limit("30/minute")
async def get_recommendations(
    request: Request,
    user: dict = Depends(require_user),
    limit: int = Query(24, ge=1, le=50, description="Max recommendations to return"),
):
    """Across anime, shows and movies, ranked by the genres of what the member
    saved and watched."""
    try:
        result = await asyncio.to_thread(service.recommend, user["user_id"], limit)
    except Exception as e:
        logger.error(f"recommendations failed: {e}")
        raise HTTPException(status_code=500, detail="Could not build recommendations")
    return {
        "success": True,
        "count": len(result["recommendations"]),
        "based_on": result["based_on"],
        "recommendations": result["recommendations"],
    }


@router.get("/recommendations/similar/{anilist_id}")
@limiter.limit("60/minute")
async def get_similar(
    request: Request,
    anilist_id: int,
    limit: int = Query(20, ge=1, le=50, description="Max recommendations to return"),
):
    """Uses no account data, so any overview page can show it. 404 when the
    title has no genres on record."""
    try:
        items = await asyncio.to_thread(service.similar, anilist_id, limit)
    except Exception as e:
        logger.error(f"similar recommendations failed: {e}")
        raise HTTPException(status_code=500, detail="Could not build recommendations")
    if items is None:
        raise HTTPException(status_code=404, detail="No genre data for that title")
    return {"success": True, "anilist_id": anilist_id, "count": len(items), "recommendations": items}
