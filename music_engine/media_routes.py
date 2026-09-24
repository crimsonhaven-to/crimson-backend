"""The audio and cover files. Public in the login wall because ``<audio>`` and
the lock screen's artwork fetch cannot send a bearer; each link is signed and
expires instead (see ``links``)."""

import asyncio
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from . import fs, links
from .db import STATUS_READY, store

router = APIRouter(tags=["music"])

# Private: a shared cache must not keep a file a signature was needed for.
_CACHE = {"Cache-Control": "private, max-age=86400"}


async def _file(kind: str, track_id: int, expires: int, signature: str) -> str:
    if not links.verify(kind, track_id, expires, signature):
        raise HTTPException(status_code=404, detail="Not found")
    track = await asyncio.to_thread(store.get_track, track_id)
    rel: Optional[str] = None
    if track and track["status"] == STATUS_READY:
        rel = track["rel_path"] if kind == links.STREAM else track["cover_path"]
    path = fs.absolute(rel) if rel else None
    if not path or not await asyncio.to_thread(os.path.isfile, path):
        raise HTTPException(status_code=404, detail="Not found")
    return path


@router.get("/music_stream/{track_id}")
async def music_stream(track_id: int, e: int = Query(...), s: str = Query(...)):
    path = await _file(links.STREAM, track_id, e, s)
    # FileResponse answers Range, so seeking and the lock screen scrubber work.
    return FileResponse(path, media_type="audio/mp4", headers=_CACHE)


@router.get("/music_art/{track_id}")
async def music_art(track_id: int, e: int = Query(...), s: str = Query(...)):
    path = await _file(links.ART, track_id, e, s)
    return FileResponse(path, media_type="image/jpeg", headers=_CACHE)
