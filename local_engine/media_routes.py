"""The local library's files: direct play, on-the-fly HLS and artwork.

A path token maps back to a file only while it sits inside a currently enabled
root, re-checked on every request, so traversal, symlink escapes and disabled
sources all 404. /local_proxy and /local_art are public because a <video> or
<img> cannot carry the bearer; /local_art is signed as well.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from . import transcode
from .fs import art_media_type_for, media_type_for, safe_resolve, safe_resolve_art, safe_resolve_transcode

logger = logging.getLogger("crimson.local.media")

router = APIRouter(tags=["local"])

_PLAYLISTS = ("master.m3u8", "media.m3u8", "index.m3u8")


@router.get("/local_proxy/{token}")
async def local_proxy(token: str):
    real_path = await asyncio.to_thread(safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    # FileResponse handles Range, so the player can seek.
    return FileResponse(real_path, media_type=media_type_for(real_path))


@router.get("/local_hls/{token}/{resource}")
async def local_hls(token: str, resource: str):
    """The VOD playlist or one ``segN.ts`` of a file that will not direct-play.
    Disabling the root or its encoding switch 404s these at once."""
    real_path = await asyncio.to_thread(safe_resolve_transcode, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    duration = await asyncio.to_thread(transcode.probe_duration, real_path)
    if not duration:
        raise HTTPException(status_code=422, detail="Could not probe media")

    if resource in _PLAYLISTS:
        return Response(content=transcode.build_media_playlist(duration), media_type="application/vnd.apple.mpegurl")

    if not (resource.startswith("seg") and resource.endswith(".ts")):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        index = int(resource[3:-3])
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not 0 <= index < transcode.segment_count(duration):
        raise HTTPException(status_code=404, detail="Not found")
    data, err = await transcode.transcode_segment(real_path, index)
    if data is None:
        logger.warning(f"[local_hls] segment {index} failed for {real_path!r}: {err}")
        raise HTTPException(status_code=502, detail="Transcode failed")
    return Response(content=data, media_type="video/mp2t")


@router.get("/local_art")
async def local_art(
    f: str = Query(..., description="base64url path token of a local artwork file"),
    s: str = Query(..., description="HMAC signature"),
):
    real_path = await asyncio.to_thread(safe_resolve_art, f, s)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        real_path,
        media_type=art_media_type_for(real_path),
        headers={"Cache-Control": "public, max-age=86400", "Access-Control-Allow-Origin": "*"},
    )
