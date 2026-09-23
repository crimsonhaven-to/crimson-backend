"""Favorites, watchlists and watch progress.

A favorite is a title in a named list ('favorites' by default; any other name is
a custom watchlist, and a title may sit in several). Progress is per episode.
"""

import csv
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from core.rate_limit import limiter
from metadata_engine import next_episode
from playback_engine.warmup import schedule_warmup

from . import transfer
from .db import QuotaExceeded, store
from .deps import require_user
from .library import dedup_by_show, favorite_item_key, progress_item_key, resolve_status
from .schemas import FavoriteIn, ProgressIn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])

# Far more than a maxed-out account's export, but bounded so a client cannot
# stream a huge body into memory.
_MAX_IMPORT_BYTES = 5 * 1024 * 1024


@router.get("/account/favorites")
def get_favorites(
    user: dict = Depends(require_user),
    list_name: Optional[str] = Query(None, description="Filter to one list; omit for all lists"),
):
    items = store.list_favorites(user["user_id"], list_name)
    return {"success": True, "count": len(items), "favorites": items}


@router.get("/account/watchlists")
def get_watchlists(user: dict = Depends(require_user)):
    lists = store.list_watchlists(user["user_id"])
    return {"success": True, "count": len(lists), "watchlists": lists}


@router.get("/account/favorites/export")
def export_favorites(
    user: dict = Depends(require_user),
    format: str = Query("csv", pattern="^(csv|json)$", description="csv (default) or json"),
):
    """Every watchlist as one attachment. CSV opens in a spreadsheet; JSON keeps
    types and nulls."""
    rows = store.list_favorites(user["user_id"])
    now = datetime.now(timezone.utc)
    filename = f"crimson-watchlists-{now.strftime('%Y%m%d')}.{format}"
    if format == "json":
        body, media_type = transfer.export_json(rows, now), "application/json"
    else:
        body, media_type = transfer.export_csv(rows), "text/csv; charset=utf-8"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/account/favorites/import")
@limiter.limit("6/minute")
async def import_favorites(
    request: Request,
    user: dict = Depends(require_user),
    mode: str = Query(
        "merge",
        pattern="^(merge|replace)$",
        description="merge (default) adds to your existing lists; replace clears all your lists first",
    ),
):
    """Restore watchlists from an export, sent as the raw body to keep the image
    free of a multipart dependency. Re-importing is idempotent. Rows with no id,
    or past the account cap, are counted in ``skipped``."""
    raw = await request.body()
    if len(raw) > _MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large to import (max 5 MB)")
    try:
        rows = transfer.parse_export(raw)
    except (json.JSONDecodeError, csv.Error, UnicodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Couldn't read that file. Upload a Crimson watchlist CSV or JSON export",
        )

    favs, skipped_no_id = transfer.favorites_from_rows(rows)
    result = await run_in_threadpool(
        store.bulk_upsert_favorites, user["user_id"], favs, mode == "replace"
    )
    return {
        "success": True,
        "mode": mode,
        "total": len(rows),
        "imported": result["imported"],
        "skipped": skipped_no_id + result["skipped_quota"],
        "skipped_no_id": skipped_no_id,
        "skipped_quota": result["skipped_quota"],
    }


@router.post("/account/favorites")
@limiter.limit("60/minute")
def add_favorite(request: Request, body: FavoriteIn, user: dict = Depends(require_user)):
    fav = {
        "item_key": favorite_item_key(body.tmdb_id, body.anilist_id, body.media_type),
        **body.model_dump(exclude={"list_name"}),
    }
    try:
        saved = store.upsert_favorite(user["user_id"], fav, list_name=body.list_name)
    except QuotaExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True, "favorite": saved}


@router.delete("/account/favorites")
def remove_favorite(
    user: dict = Depends(require_user),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    item_key: Optional[str] = Query(None),
    media_type: Optional[str] = Query(None, description="'movie' to target the movie namespace"),
    list_name: Optional[str] = Query(None, description="Remove from one list; omit for all lists"),
):
    if not item_key:
        if tmdb_id is None and anilist_id is None:
            raise HTTPException(status_code=400, detail="Provide item_key, tmdb_id or anilist_id")
        item_key = favorite_item_key(tmdb_id, anilist_id, media_type)
    if not store.remove_favorite(user["user_id"], item_key, list_name):
        raise HTTPException(status_code=404, detail="Favorite not found")
    return {"success": True, "removed": item_key}


@router.get("/account/progress")
def get_progress(
    user: dict = Depends(require_user),
    status: Optional[str] = Query(
        None, pattern="^(in_progress|completed)$", description="Filter: in_progress | completed"
    ),
):
    items = store.list_progress(user["user_id"], status=status)
    return {"success": True, "count": len(items), "progress": items}


@router.post("/account/progress")
@limiter.limit("60/minute")
async def upsert_progress(request: Request, body: ProgressIn, user: dict = Depends(require_user)):
    item_key = progress_item_key(
        body.tmdb_id,
        body.anilist_id,
        body.season_number,
        body.episode_number,
        body.media_type,
        body.local_id,
    )
    payload = {**body.model_dump(), "item_key": item_key, "status": resolve_status(body)}
    try:
        prog = await run_in_threadpool(store.upsert_progress, user["user_id"], payload)
    except QuotaExceeded as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Pre-cache the next episode so it plays instantly. Movies, manga and local
    # media have no next episode to fetch.
    if (
        body.media_type not in ("movie", "manga", "local")
        and body.tmdb_id is not None
        and body.season_number is not None
        and body.episode_number is not None
    ):
        try:
            prefs = await run_in_threadpool(store.get_preferences, user["user_id"])
            schedule_warmup(
                request,
                tmdb_id=body.tmdb_id,
                season_number=body.season_number,
                episode_number=body.episode_number,
                preferences=prefs,
            )
        except Exception as e:
            logger.warning(f"warmup scheduling failed: {e}")

    return {"success": True, "progress": prog}


@router.get("/account/continue-watching")
async def continue_watching(user: dict = Depends(require_user)):
    """In-progress titles, most recent first, each at its latest episode."""
    rows = await run_in_threadpool(store.list_progress, user["user_id"], status="in_progress")
    items = await next_episode.annotate(dedup_by_show(rows))
    return {"success": True, "count": len(items), "items": items}


@router.get("/account/recent")
async def recent(
    user: dict = Depends(require_user),
    limit: int = Query(20, ge=1, le=100, description="Max items to return"),
):
    """Like continue-watching, but keeps completed titles, so history stays
    populated after a series is finished."""
    rows = await run_in_threadpool(store.list_progress, user["user_id"])
    items = await next_episode.annotate(dedup_by_show(rows, limit=limit))
    return {"success": True, "count": len(items), "items": items}


@router.delete("/account/progress")
def remove_progress(
    user: dict = Depends(require_user),
    item_key: Optional[str] = Query(None),
    tmdb_id: Optional[int] = Query(None),
    anilist_id: Optional[int] = Query(None),
    season_number: Optional[int] = Query(None),
    episode_number: Optional[int] = Query(None),
    media_type: Optional[str] = Query(None, description="'movie'/'local' to target that namespace"),
    local_id: Optional[str] = Query(None, description="on-disk title token (media_type='local')"),
):
    if not item_key:
        if tmdb_id is None and anilist_id is None and local_id is None:
            raise HTTPException(
                status_code=400,
                detail="Provide item_key, or tmdb_id/anilist_id/local_id (+season/episode)",
            )
        item_key = progress_item_key(
            tmdb_id, anilist_id, season_number, episode_number, media_type, local_id
        )
    if not store.remove_progress(user["user_id"], item_key):
        raise HTTPException(status_code=404, detail="Progress entry not found")
    return {"success": True, "removed": item_key}
