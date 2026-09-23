"""The directories the operator exposes as local sources, typically a NAS share
or a bind-mount."""

import asyncio
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from account_engine.deps import require_admin
from download_engine.fs import inspect_downloads

from .db import store
from .fs import discover_mountpoints, inspect_path
from .transcode import tools_available

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class LocalSourceCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    path: str = Field(..., min_length=1, max_length=1000)
    # On-the-fly HLS for files that will not direct-play. Opt-in per source.
    encoding: bool = False
    # Whether the downloader may write into this root, under crimson-downloads/.
    download_enabled: bool = False


class LocalSourceUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None
    encoding: Optional[bool] = None
    download_enabled: Optional[bool] = None


def _with_status(row: dict) -> dict:
    """The stored row plus a live filesystem probe, and free space for a root the
    downloader may pick."""
    out = {
        **row,
        "enabled": bool(row.get("enabled")),
        "encoding": bool(row.get("encoding")),
        "download_enabled": bool(row.get("download_enabled")),
        "status": inspect_path(row["path"]),
    }
    if out["download_enabled"]:
        out["downloads"] = inspect_downloads(row["path"])
    return out


def _checked_new_path(raw: str) -> str:
    """Absolute, existing and readable inside the container, checked here so a
    missing bind-mount fails loudly rather than resolving nothing later."""
    path = os.path.normpath(raw.strip())
    if not os.path.isabs(path):
        raise HTTPException(status_code=400, detail="Path must be absolute: the in-container path, e.g. /crimson/movies1")
    info = inspect_path(path)
    if not info["exists"]:
        raise HTTPException(
            status_code=400,
            detail="Path does not exist inside the backend container. Bind-mount it in "
                   "docker-compose first (e.g. - /movies:/crimson/movies1).",
        )
    if not info["is_dir"]:
        raise HTTPException(status_code=400, detail="Path is not a directory")
    if not info["readable"]:
        raise HTTPException(status_code=400, detail="Path is not readable by the backend")
    if any(os.path.normpath(r["path"]) == path for r in store.list_sources()):
        raise HTTPException(status_code=409, detail="That path is already registered")
    return path


@router.get("/local-sources")
async def list_local_sources():
    items = await asyncio.to_thread(lambda: [_with_status(r) for r in store.list_sources()])
    return {
        "success": True,
        "count": len(items),
        "sources": items,
        # Encoding needs ffmpeg and ffprobe; without them the toggle greys out.
        "encoding_supported": tools_available(),
    }


@router.get("/local-sources/discover")
async def discover_local_sources():
    """Directories visible inside the container, addable in one click."""
    def _discover():
        have = {os.path.normpath(r["path"]) for r in store.list_sources()}
        return [{**m, "already_added": os.path.normpath(m["path"]) in have} for m in discover_mountpoints()]

    mounts = await asyncio.to_thread(_discover)
    return {"success": True, "count": len(mounts), "mounts": mounts}


@router.post("/local-sources")
async def add_local_source(body: LocalSourceCreate):
    def _add():
        path = _checked_new_path(body.path)
        return _with_status(store.add_source(body.label.strip(), path, body.encoding, body.download_enabled))

    return {"success": True, "source": await asyncio.to_thread(_add)}


@router.patch("/local-sources/{source_id}")
async def update_local_source(source_id: int, body: LocalSourceUpdate):
    """The path is immutable: delete and re-add to move a source."""
    def _update():
        if not store.get_source(source_id):
            raise HTTPException(status_code=404, detail="Source not found")
        label = body.label.strip() if body.label is not None else None
        return _with_status(store.update_source(source_id, label, body.enabled, body.encoding, body.download_enabled))

    return {"success": True, "source": await asyncio.to_thread(_update)}


@router.delete("/local-sources/{source_id}")
async def delete_local_source(source_id: int):
    if not await asyncio.to_thread(store.delete_source, source_id):
        raise HTTPException(status_code=404, detail="Source not found")
    return {"success": True, "deleted": source_id}
