"""Same-origin stream proxies for the operator-owned sources + the backend player.

Several sources hand the player a same-origin proxy path instead of a raw CDN URL,
because the CDN gates segments on a Referer/Origin/UA/ASN the viewer's browser
can't satisfy (or serves no usable CORS). Every proxy ends the same way: turn the
resolver ``proxy_fetch`` result (status, content_type, headers, payload) into the
right response — buffered bytes for a rewritten HLS playlist, a streamed body
(Range/length headers forwarded) for a media segment.

These are the ONLY stream proxies the base backend serves (third-party source
proxies were removed with their scrapers). The optional build-time overlay's
proxies are registered separately in ``api.py`` (schema-hidden, name-derived), and
reuse ``_proxy_response`` from here.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from resolvers.jellyfin import proxy_fetch as jellyfin_proxy_fetch
from resolvers.febbox import proxy_fetch as febbox_proxy_fetch
from local_engine.fs import (
    safe_resolve as local_safe_resolve,
    safe_resolve_transcode as local_safe_resolve_transcode,
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
    """Shape a resolver ``proxy_fetch`` result into a Response/StreamingResponse.

    ``payload`` is either rewritten ``bytes`` (an HLS playlist) or an async byte
    iterator (a streamed media segment). Bytes responses don't forward upstream
    headers unless ``forward_bytes_headers`` is set (Jellyfin needs them)."""
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


# --- JELLYFIN PROXY ("jellyfin" source) ---
@router.api_route("/jellyfin_proxy/{path:path}", methods=["GET", "POST"])
async def jellyfin_proxy(request: Request, path: str):
    """Authenticated reverse proxy to the user's Jellyfin server. Injects the
    access token server-side (so it never reaches the browser) and rewrites HLS
    playlists to flow back through this proxy; media segments / direct files are
    streamed straight through with Range passthrough. Configured via the
    JELLYFIN_* env vars (see resolvers.jellyfin)."""
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
    # Jellyfin forwards upstream headers on buffered (playlist) responses too.
    return _proxy_response(*result, forward_bytes_headers=True)


# --- FEBBOX SUBTITLE PROXY (operator-only /resolve grant) ---
# Not part of the public /watch pipeline. The /resolve grant returns Febbox's
# the tiny .srt
# subtitles are minted as signed /febbox_proxy paths, which this route fetches and
# converts to WebVTT. HMAC-signed (no open relay) and inert unless FEBBOX_UI_TOKEN
# is configured.
@router.get("/febbox_proxy", include_in_schema=False)
async def febbox_proxy(request: Request):
    try:
        result = await febbox_proxy_fetch(
            url=request.query_params.get("u"),
            sig=request.query_params.get("s"),
            range_header=request.headers.get("range"),
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except httpx.RequestError as e:
        logger.error(f"Febbox proxy upstream error: {e}")
        raise HTTPException(status_code=502, detail="Upstream fetch failed")
    return _proxy_response(*result)


# --- LOCAL SOURCE PROXY ("Local" source: admin-registered dirs / NAS) ---
@router.get("/local_proxy/{token}")
async def local_proxy(token: str):
    """Stream a browser-playable file from an admin-registered local source.

    ``token`` is an opaque base64url of the absolute path the LocalScraper found.
    ``safe_resolve`` maps it back to a real file ONLY when it currently lives
    inside an *enabled* source root (path traversal / symlink escapes / disabled
    sources all resolve to None → 404), re-checked on every request. Starlette's
    FileResponse handles HTTP Range requests, so the player can seek."""
    real_path = await run_in_threadpool(local_safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real_path, media_type=local_media_type(real_path))


@router.get("/local_hls/{token}/{resource}")
async def local_hls(token: str, resource: str):
    """On-the-fly HLS for a transcodable Local file (mkv/avi/ts/…) whose source has
    encoding enabled — the non-direct-play counterpart of /local_proxy.

    ``resource`` is either the VOD playlist (``master.m3u8``/``media.m3u8``) or a
    segment (``seg{n}.ts``). ``safe_resolve_transcode`` re-validates on EVERY request
    that the token maps to a transcodable file inside a *currently enabled* source
    root with **encoding on** — so disabling the source (or just its encoding) instantly
    404s its transcode streams, exactly like /local_proxy for direct play. Gated by the
    login wall (NOT a public prefix), so the player must carry the session token; the
    bytes never leave this host's library unauthenticated."""
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


@router.get("/cache_proxy/{token}")
async def cache_proxy(token: str):
    """Stream a server-side-cached episode straight off the NAS.

    ``token`` is an opaque base64url of the cached file's absolute path.
    ``cache_safe_resolve`` maps it back to a real file ONLY when it currently
    lives inside an *enabled* cache target (traversal/symlink escapes / disabled
    targets all 404), re-checked per request. FileResponse handles Range so the
    player can seek. Mirrors /local_proxy."""
    real_path = await run_in_threadpool(cache_safe_resolve, token)
    if not real_path:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(real_path, media_type=cache_media_type(real_path))


# --- BACKEND-HOSTED PLAYER (Crimson-themed hls.js/mp4 player) ---
@router.get("/player")
async def player(
    src: str = Query(..., description="Same-origin stream path to play"),
    stream_type: str = Query("", alias="type", description="hls or mp4 (inferred if omitted)"),
    title: str = Query("", description="Optional title"),
):
    """Serve a Crimson-themed player for a same-origin proxied stream. Resolvers
    that return a raw hls/mp4 stream (e.g. Jellyfin) wrap it in this page so the
    frontend can iframe it like any other source. ``src`` is restricted to
    same-origin relative paths to prevent embedding arbitrary external content."""
    if not is_safe_src(src):
        raise HTTPException(status_code=400, detail="Invalid src (must be a same-origin path)")
    html = render_player(src=src, stream_type=stream_type, title=title)
    return Response(content=html, media_type="text/html; charset=utf-8")
