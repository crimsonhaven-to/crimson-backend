"""The operator's view of the music library: every song on the share, whose
playlists hold it, and how much room it all takes."""

import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request

from account_engine.deps import require_admin
from core.public_url import public_base_url

from . import cdn
from .db import store
from .payloads import track_payload

router = APIRouter(prefix="/admin/music", tags=["admin"], dependencies=[Depends(require_admin)])

Status = Literal["pending", "working", "ready", "review", "unmatched", "failed"]


@router.get("/library")
async def library(
    request: Request,
    q: str = Query("", max_length=200),
    status: Optional[Status] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    summary, (rows, total) = await asyncio.gather(
        asyncio.to_thread(store.library_summary),
        asyncio.to_thread(
            store.library_page, query=q.strip(), status=status, limit=limit, offset=offset
        ),
    )
    base = public_base_url(request).rstrip("/")
    return {
        "summary": {**summary, "cdn": cdn.enabled()},
        "total": total,
        "tracks": [
            {
                **track_payload(row, base),
                "rel_path": row["rel_path"],
                "mirrored": row["mirrored_at"] is not None,
                "created_at": row["created_at"],
                "owners": row["owners"],
                "playlist_count": row["playlist_count"],
            }
            for row in rows
        ],
    }
