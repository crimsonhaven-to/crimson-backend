"""Same-origin stream proxies for the operator-owned sources, plus the player.

Several sources hand the player a same-origin proxy path rather than a raw CDN
URL, because the CDN gates segments on a Referer, UA or ASN the viewer's browser
cannot satisfy, or serves no usable CORS. Every proxy ends the same way: turn the
resolver's ``proxy_fetch`` result into the right response, buffered bytes for a
rewritten HLS playlist or a streamed body with Range forwarded for a segment.

These are the only stream proxies a base build serves. The overlay's are
registered separately in api.py and reuse ``_proxy_response`` from here.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from resolvers.jellyfin import proxy_fetch as jellyfin_proxy_fetch
from local_engine.fs import (
    safe_resolve as local_safe_resolve,
    safe_resolve_transcode as local_safe_resolve_transcode,
    safe_resolve_art as local_safe_resolve_art,
    art_media_type_for as local_art_media_type,
    media_type_for as local_media_type,
)
from local_engine import transcode as local_transcode
from cache_engine.fs import (
    safe_resolve as cache_safe_resolve,
    media_type_for as cache_media_type,
)
from core.player import render_player, is_safe_src

logger = logging.getLogger("crimson.proxies")

router = APIRouter()


def _proxy_response(status, content_type, headers, payload, *, forward_bytes_headers=False):
    """Shape a resolver ``proxy_fetch`` result into a Response.

    ``payload`` is either rewritten bytes for an HLS playlist or an async byte
    iterator for a streamed segment. Bytes responses forward upstream headers only
    under ``forward_bytes_headers``, which Jellyfin needs."""
    if isinstance(payload, (bytes, bytearray)):
        return Response(
            content=payload,
            status_code=status,
            media_type=content_type,
            headers=headers if forward_bytes_headers else None,
        )
    return StreamingResponse(
        payload, status_code=status, media_type=content_type, headers=headers
    )


# --- JELLYFIN PROXY ---
@router.api_route("/jellyfin_proxy/{path:path}", methods=["GET", "POST"])
async def jellyfin_proxy(request: Request, path: str):
    """Authenticated reverse proxy to the user's Jellyfin server.

    Injects the access token server-side so it never reaches the browser, and
    rewrites HLS playlists to flow back through this proxy. Segments and direct
    files stream through with Range passthrough."""
    body = await request.body() if request.method == "POST" else None
    try:
        result = await jellyfin_proxy_fetch(
            path=path,
            query_string=request.url.query,
            method=request.method,
            body=body,
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Jellyfin proxy upstream error for {path}: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")
    # Jellyfin needs upstream headers on buffered playlist responses too.
    return _proxy_response(*result, forward_bytes_headers=True)


# An overlay source's own proxy is not wired here: api.py auto-registers it when
# the build ships a module with a ``proxy_fetch``. So this file names no overlay
# source, and a base build serves only the operator-owned proxies below.


# --- LOCAL SOURCE PROXY (admin-registered dirs / NAS) ---
@router.get("/local_proxy/{token}")
async def local_proxy(token: str):
    """Stream a browser-playable file from an admin-registered local source.

    ``token`` is an opaque base64url of the path the scraper found.
    ``safe_resolve`` maps it back only when it currently sits inside an *enabled*
    root, re-checked every request, so traversal, symlink escapes and disabled
    sources all 404. FileResponse handles Range, so the player can seek."""
    real_path = await run_in_threadpool(local_safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real_path, media_type=local_media_type(real_path))


@router.get("/local_hls/{token}/{resource}")
async def local_hls(token: str, resource: str):
    """On-the-fly HLS for a transcodable Local file, the counterpart of
    /local_proxy for anything that will not direct-play.

    ``resource`` is the VOD playlist or a segment. Every request re-validates that
    the token maps to a transcodable file inside a currently enabled root with
    encoding on, so disabling either instantly 404s its transcode streams. Gated
    by the login wall rather than a public prefix, so the bytes never leave this
    host unauthenticated."""
    real_path = await run_in_threadpool(local_safe_resolve_transcode, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")

    duration = await run_in_threadpool(local_transcode.probe_duration, real_path)
    if not duration:
        raise HTTPException(status_code=422, detail="Could not probe media")

    if resource in ("master.m3u8", "media.m3u8", "index.m3u8"):
        playlist = local_transcode.build_media_playlist(duration)
        return Response(content=playlist, media_type="application/vnd.apple.mpegurl")

    if resource.startswith("seg") and resource.endswith(".ts"):
        try:
            index = int(resource[3:-3])
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found")
        if index < 0 or index >= local_transcode.segment_count(duration):
            raise HTTPException(status_code=404, detail="Not found")
        data, err = await local_transcode.transcode_segment(real_path, index)
        if data is None:
            logger.warning(f"[local_hls] segment {index} failed for {real_path!r}: {err}")
            raise HTTPException(status_code=502, detail="Transcode failed")
        return Response(content=data, media_type="video/mp2t")

    raise HTTPException(status_code=404, detail="Not found")


@router.get("/local_art")
async def local_art(
    f: str = Query(..., description="base64url path token of a local artwork file"),
    s: str = Query(..., description="HMAC signature"),
):
    """Serve a poster or cover image found next to a local title.

    Public, because an ``<img>`` cannot carry the login-wall bearer, so the path
    token is HMAC-signed and a forged one is rejected. Every request then
    re-validates that it maps to a real image inside a currently enabled root,
    exactly like /local_proxy does for video."""
    real_path = await run_in_threadpool(local_safe_resolve_art, f, s)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        real_path,
        media_type=local_art_media_type(real_path),
        headers={"Cache-Control": "public, max-age=86400", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/cache_proxy/{token}")
async def cache_proxy(token: str):
    """Stream a server-side-cached episode straight off the NAS.

    Mirrors /local_proxy: the token maps back to a file only while it sits inside
    an *enabled* cache target, re-checked per request, and Range is handled so the
    player can seek."""
    real_path = await run_in_threadpool(cache_safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real_path, media_type=cache_media_type(real_path))


# --- BACKEND-HOSTED PLAYER ---
@router.get("/player")
async def player(
    src: str = Query(..., description="Same-origin stream path to play"),
    stream_type: str = Query("", alias="type", description="hls or mp4 (inferred if omitted)"),
    title: str = Query("", description="Optional title"),
):
    """A themed player page for a same-origin proxied stream.

    Resolvers returning a raw hls/mp4 stream wrap it in this page so the frontend
    can iframe it like any other source. ``src`` is restricted to same-origin
    relative paths, so it cannot embed arbitrary external content."""
    if not is_safe_src(src):
        raise HTTPException(status_code=400, detail="Invalid src (must be a same-origin path)")
    html = render_player(src=src, stream_type=stream_type, title=title)
    return Response(content=html, media_type="text/html; charset=utf-8")
