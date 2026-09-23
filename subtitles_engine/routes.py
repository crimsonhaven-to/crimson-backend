"""Subtitle routes.

| route                    | access         | cost                               |
|--------------------------|----------------|------------------------------------|
| ``GET /subtitles``       | login wall     | a search, no download quota        |
| ``GET /subtitles_proxy`` | public, signed | one download per file, then cached |

The proxy is public because a ``<track>`` loads cross-origin without the bearer;
the HMAC on the file id keeps it from being driven to spend quota.
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
    """Each ``url`` is a signed ``/subtitles_proxy`` path for a ``<track>``. 503
    without ``OPENSUBTITLES_API_KEY``; upstream trouble is an empty list."""
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
    """One subtitle file as WebVTT."""
    if not service.configured():
        raise HTTPException(status_code=503, detail="Subtitles are not configured")
    if not service.verify(f, s):
        raise HTTPException(status_code=403, detail="Bad or missing signature")

    vtt = await service.fetch_vtt(f)
    if vtt is None:
        raise HTTPException(status_code=502, detail="Subtitle unavailable")

    return Response(
        content=vtt,
        media_type="text/vtt; charset=utf-8",
        headers={
            # A signed id maps to immutable content.
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )
