"""The cache's master switch, its NAS targets and the ledger of cached episodes.
When on, the episode a viewer settles on is remuxed to mp4 onto the first
writable target and replays as the Cache source."""

import asyncio
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from account_engine.deps import require_admin
from core.config import get_settings
from local_engine.fs import discover_mountpoints

from .db import store
from .downloader import ffmpeg_available
from .fs import inspect_target

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class CacheSettingsUpdate(BaseModel):
    enabled: bool


class CacheTargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    path: str = Field(..., min_length=1, max_length=1000)


class CacheTargetUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None


def _with_status(row: dict) -> dict:
    return {**row, "enabled": bool(row.get("enabled")), "status": inspect_target(row["path"])}


def _checked_new_path(raw: str) -> str:
    path = os.path.normpath(raw.strip())
    if not os.path.isabs(path):
        raise HTTPException(status_code=400, detail="Path must be absolute: the in-container path, e.g. /crimson/cache")
    info = inspect_target(path, 1)
    if not info["exists"]:
        raise HTTPException(
            status_code=400,
            detail="Path does not exist inside the backend container. Bind-mount your NAS "
                   "share first (e.g. - /nas/cache:/crimson/cache).",
        )
    if not info["is_dir"]:
        raise HTTPException(status_code=400, detail="Path is not a directory")
    if not info["writable"]:
        raise HTTPException(status_code=400, detail="Path is not writable by the backend")
    if any(os.path.normpath(r["path"]) == path for r in store.list_targets()):
        raise HTTPException(status_code=409, detail="That path is already registered")
    return path


@router.get("/cache")
async def cache_overview():
    def _read():
        return store.get_enabled(), store.stats(), len(store.enabled_targets())

    enabled, stats, target_count = await asyncio.to_thread(_read)
    settings = get_settings()
    return {
        "success": True,
        "enabled": enabled,
        "ffmpeg_available": ffmpeg_available(),
        "enabled_targets": target_count,
        "stats": stats,
        "config": {
            "max_concurrent": settings.cache_max_concurrent,
            "download_timeout": settings.cache_download_timeout,
            "min_free_bytes": settings.cache_min_free_bytes,
            "internal_base": settings.cache_internal_base,
        },
    }


@router.put("/cache/settings")
async def update_cache_settings(body: CacheSettingsUpdate):
    """Off stops new downloads; cached episodes keep playing while their target
    stays enabled."""
    return {"success": True, "enabled": await asyncio.to_thread(store.set_enabled, body.enabled)}


@router.get("/cache-targets")
async def list_cache_targets():
    items = await asyncio.to_thread(lambda: [_with_status(r) for r in store.list_targets()])
    return {"success": True, "count": len(items), "targets": items}


@router.get("/cache-targets/discover")
async def discover_cache_targets():
    """Candidate directories, probed for writability and free space."""
    def _discover():
        have = {os.path.normpath(r["path"]) for r in store.list_targets()}
        return [
            {
                "path": m["path"],
                "fstype": m.get("fstype"),
                **inspect_target(m["path"], count_cap=1),
                "already_added": os.path.normpath(m["path"]) in have,
            }
            for m in discover_mountpoints()
        ]

    mounts = await asyncio.to_thread(_discover)
    return {"success": True, "count": len(mounts), "mounts": mounts}


@router.post("/cache-targets")
async def add_cache_target(body: CacheTargetCreate):
    def _add():
        path = _checked_new_path(body.path)
        return _with_status(store.add_target(body.name.strip(), path))

    return {"success": True, "target": await asyncio.to_thread(_add)}


@router.patch("/cache-targets/{target_id}")
async def update_cache_target(target_id: int, body: CacheTargetUpdate):
    """The name is what viewers see as the source. The path is immutable."""
    def _update():
        if not store.get_target(target_id):
            raise HTTPException(status_code=404, detail="Target not found")
        name = body.name.strip() if body.name is not None else None
        return _with_status(store.update_target(target_id, name, body.enabled))

    return {"success": True, "target": await asyncio.to_thread(_update)}


@router.delete("/cache-targets/{target_id}")
async def delete_cache_target(target_id: int):
    """Its ledger rows cascade, but the files stay on the share."""
    if not await asyncio.to_thread(store.delete_target, target_id):
        raise HTTPException(status_code=404, detail="Target not found")
    return {"success": True, "deleted": target_id}


@router.get("/cached-episodes")
async def list_cached_episodes(
    status: Optional[str] = Query(None, description="ready / pending / downloading / failed"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    def _read():
        return store.list_episodes(status, limit, offset), store.count_episodes(status)

    items, total = await asyncio.to_thread(_read)
    return {"success": True, "count": len(items), "total": total, "episodes": items}


@router.delete("/cached-episodes/{entry_id}")
async def delete_cached_episode(entry_id: int):
    """Drops the entry and its file. Dropping a failed entry lets the episode be
    cached again on its next play."""
    def _delete():
        row = store.delete_episode(entry_id)
        if not row:
            raise HTTPException(status_code=404, detail="Cache entry not found")
        target = store.get_target(row["target_id"])
        if target:
            try:
                os.unlink(os.path.join(target["path"], row["rel_path"]))
            except OSError:
                pass

    await asyncio.to_thread(_delete)
    return {"success": True, "deleted": entry_id}
