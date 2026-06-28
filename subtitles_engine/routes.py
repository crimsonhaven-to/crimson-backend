"""
Subtitle API — OpenSubtitles-backed external tracks for the in-app player.

Two endpoints, mirroring how the stream sources split "find" (gated, authed XHR)
from "fetch" (public, signed, browser-loadable):

  * ``GET /subtitles``        — authed. Returns ``[{url, lang, label}]`` for a
    title; the frontend merges these into CrimsonPlayer's ``subtitles`` prop. Cheap
    (no download quota spent — just a search). Behind the login wall.
  * ``GET /subtitles_proxy``  — PUBLIC + signed. The ``<track>`` element loads its
    ``src`` cross-origin with ``crossOrigin="anonymous"`` (no auth header can ride
    along), exactly like the stream proxies, so this can't be gated by the login
    wall — it's protected by the HMAC on the ``f`` (file_id) instead. This is the
    only step that spends OpenSubtitles download quota; results are cached.

See ``subtitles_engine.service`` for the OpenSubtitles client, quota handling and
the SRT→VTT conversion.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .service import service

router = APIRouter(tags=["subtitles"])


def _parse_langs(languages: str) -> list:
    return [p.strip().lower() for p in (languages or "").split(",") if p.strip()][:8]


@router.get("/subtitles")
async def list_subtitles(
    tmdb_id: int = Query(..., description="TMDB id (show id for episodes, movie id for movies)"),
    languages: str = Query("en", description="Comma-separated 2-letter language codes, e.g. en,de"),
    season: Optional[int] = Query(None, ge=0),
    episode: Optional[int] = Query(None, ge=0),
    is_movie: bool = Query(False),
):
    """External subtitle tracks for a title from OpenSubtitles.

    Returns ``{success, subtitles: [{url, lang, label}]}``; ``url`` is a signed,
    same-origin ``/subtitles_proxy`` path the player can hand straight to a
    ``<track>``. Returns 503 when ``OPENSUBTITLES_API_KEY`` isn't configured.
    Never raises on upstream trouble — an empty list just means "no tracks found"."""
    if not service.configured():
        raise HTTPException(status_code=503, detail="Subtitles are not configured")
    tracks = await service.search(
        tmdb_id=tmdb_id,
        languages=_parse_langs(languages),
        season=season,
        episode=episode,
        is_movie=is_movie,
    )
    return {"success": True, "count": len(tracks), "subtitles": tracks}


@router.get("/subtitles_proxy")
async def subtitles_proxy(
    f: str = Query(..., description="OpenSubtitles file_id (HMAC-signed)"),
    s: str = Query(..., description="signature"),
):
    """Download + convert one subtitle file to WebVTT and serve it same-origin.

    Public (the ``<track>`` can't send auth) but the ``f`` file_id is HMAC-signed —
    an unsigned/forged id is rejected so the proxy can't be driven to drain our
    OpenSubtitles download quota. The converted VTT is cached by file_id, so a
    re-selected track / second viewer doesn't spend quota again."""
    if not service.configured():
        raise HTTPException(status_code=503, detail="Subtitles are not configured")
    if not service.verify(f, s):
        raise HTTPException(status_code=403, detail="Bad or missing signature")

    vtt = await service.fetch_vtt(f)
    if vtt is None:
        # Upstream failed or quota exhausted — let the player simply show no track.
        raise HTTPException(status_code=502, detail="Subtitle unavailable")

    return Response(
        content=vtt,
        media_type="text/vtt; charset=utf-8",
        headers={
            # The signed id maps to immutable content; let the browser cache it.
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )
