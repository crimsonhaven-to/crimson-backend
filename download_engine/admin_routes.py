"""Admin downloads: an http(s) URL or magnet link fetched by the aria2 sidecar
into ``<root>/crimson-downloads/`` on the first download-enabled local source
with room, where the library scanner then finds it."""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from account_engine.audit import admin_identity
from account_engine.deps import require_admin
from core.config import get_settings
from core.rate_limit import limiter
from local_engine.db import store as local_store

from . import aria2, manager
from .db import KIND_HTTP, KIND_TORRENT, store
from .fs import inspect_downloads

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class DownloadCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=8000, description="An http(s) URL or a magnet: link")
    # Becomes the crimson-downloads/<name>/ folder, which helps the scanner
    # identify the title. Omitted keeps the release's own name.
    name: Optional[str] = Field(None, max_length=180)


def _kind(url: str) -> str:
    low = url.strip().lower()
    if low.startswith("magnet:"):
        return KIND_TORRENT
    if low.startswith(("http://", "https://")):
        if low.split("?", 1)[0].endswith(".torrent"):
            raise HTTPException(status_code=400, detail="Paste the magnet link instead of a .torrent file URL.")
        return KIND_HTTP
    raise HTTPException(status_code=400, detail="URL must be an http(s):// link or a magnet: link.")


def _job_view(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    total = row.get("bytes_total")
    done = row.get("bytes_done") or 0
    return {
        **row,
        "bytes_done": int(done),
        "bytes_total": int(total) if total else None,
        "download_speed": int(row.get("download_speed") or 0),
        "progress": done / total if total and total > 0 else None,
    }


async def _job_or_404(job_id: int) -> dict:
    job = await asyncio.to_thread(store.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download not found")
    return job


@router.get("/downloads")
async def downloads_overview():
    """aria2 availability, job counts, and the download-enabled roots in the order
    the downloader tries them."""
    def _read():
        roots = [
            {"id": r["id"], "label": r.get("label"), "path": r["path"], **inspect_downloads(r["path"])}
            for r in local_store.download_roots_config()
        ]
        return store.stats(), roots

    (stats, targets), aria2_ok = await asyncio.gather(asyncio.to_thread(_read), aria2.is_available())
    settings = get_settings()
    return {
        "success": True,
        "aria2_available": aria2_ok,
        "aria2_rpc_url": settings.aria2_rpc_url,
        "download_targets": targets,
        "stats": stats,
        "config": {
            "max_active": settings.download_max_active,
            "min_free_bytes": settings.download_min_free_bytes,
            "poll_interval": settings.download_poll_interval,
        },
    }


@router.get("/download-jobs")
async def list_download_jobs(
    status: Optional[str] = Query(None, description="pending / active / paused / complete / failed"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    def _read():
        return store.list_jobs(status, limit, offset), store.count_jobs(status)

    rows, total = await asyncio.to_thread(_read)
    return {"success": True, "count": len(rows), "total": total, "jobs": [_job_view(r) for r in rows]}


@router.post("/downloads")
@limiter.limit("60/minute")
async def create_download(request: Request, body: DownloadCreate, user: dict = Depends(require_admin)):
    """Refused up front with no download-enabled source, rather than queueing a
    job that would never land anywhere."""
    kind = _kind(body.url)
    if not await asyncio.to_thread(local_store.download_roots_config):
        raise HTTPException(status_code=400, detail="No local source is download-enabled. Enable one under Local Sources first.")
    row = await asyncio.to_thread(
        store.create_job, kind, body.url.strip(), (body.name or "").strip() or None, admin_identity(user)
    )
    return {"success": True, "job": _job_view(row)}


@router.post("/download-jobs/{job_id}/pause")
async def pause_download(job_id: int):
    return {"success": True, "job": _job_view(await manager.pause_job(await _job_or_404(job_id)))}


@router.post("/download-jobs/{job_id}/resume")
async def resume_download(job_id: int):
    return {"success": True, "job": _job_view(await manager.resume_job(await _job_or_404(job_id)))}


@router.post("/download-jobs/{job_id}/retry")
async def retry_download(job_id: int):
    """The staging dir stays, so aria2 resumes the partial rather than restarting."""
    await _job_or_404(job_id)
    return {"success": True, "job": _job_view(await asyncio.to_thread(store.requeue, job_id))}


@router.delete("/download-jobs/{job_id}")
async def delete_download(job_id: int):
    """Cancels and removes the staging files. A finished download's published
    file stays; delete it from the Local library."""
    row = await asyncio.to_thread(store.delete_job, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")
    await manager.cancel_job(row)
    return {"success": True, "deleted": job_id}
