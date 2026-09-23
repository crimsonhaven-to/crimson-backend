"""The /mw bridge: the /watch pipeline reshaped into @movie-web/providers'
native ``Stream`` JSON, so a movie-web fork can use Crimson as one source.

These are the only routes an API key reaches. Unlike /watch the answer is one
buffered document, because movie-web wants a source's streams as a value, and
iframe sources are dropped, because movie-web has no iframe player.
"""

import re
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Request

from core.public_url import public_base_url
from core.rate_limit import limiter

from .pipeline import watch_events
from .titles import movie_title, tv_start

router = APIRouter(tags=["movie-web"])


def _slug(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "src"


def _captions(subtitles: Optional[List[Dict]]) -> List[Dict]:
    """The tracks are absolute same-origin WebVTT proxy paths, so vtt unless the
    URL says srt."""
    out: List[Dict] = []
    for i, s in enumerate(subtitles or []):
        url = s.get("url")
        if not url:
            continue
        label = s.get("label") or s.get("lang") or "Unknown"
        out.append(
            {
                "id": f"{_slug(label)}-{i}",
                "type": "srt" if ".srt" in url.lower() else "vtt",
                "url": url,
                "language": s.get("lang") or label,
                "hasCorsRestrictions": False,
            }
        )
    return out


def _to_mw_stream(line: Dict, idx: int) -> Optional[Dict]:
    stype, url = line.get("streamType"), line.get("url")
    if not url or stype == "iframe":
        return None
    base = {
        "id": f"crimson-{_slug(line.get('source'))}-{idx}",
        # No playback guarantees, so the fork routes the stream through its own
        # proxy, which is also where it injects the API key.
        "flags": [],
        "captions": _captions(line.get("subtitles")),
        "crimsonSource": line.get("source"),
        "crimsonLanguage": line.get("language"),
    }
    if stype == "hls":
        return {**base, "type": "hls", "playlist": url}
    # movie-web's file shape keys by quality, which Crimson does not probe.
    return {**base, "type": "file", "qualities": {"unknown": {"type": "mp4", "url": url}}}


async def _collect(events) -> Tuple[Dict, List[Dict]]:
    meta: Dict = {}
    streams: List[Dict] = []
    idx = 0
    async for event in events:
        kind = event.get("type")
        if kind == "meta":
            meta = {**meta, **event}
        elif kind == "unaired":
            meta = {**meta, "unaired": True, "air_date": event.get("air_date")}
        elif kind == "stream":
            mw = _to_mw_stream(event, idx)
            idx += 1
            if mw:
                streams.append(mw)
    return meta, streams


@router.get("/mw/watch/movie/{tmdb_id}")
@limiter.limit("30/minute")
async def mw_watch_movie(request: Request, tmdb_id: int):
    """Declared before the TV route, whose {tmdb_id} would otherwise take "movie"."""
    title = await movie_title(tmdb_id)
    meta, streams = await _collect(
        watch_events(
            tmdb_id,
            None,
            None,
            None,
            title,
            base_url=public_base_url(request),
            media_type="movie",
        )
    )
    return {
        "success": True,
        "media": "movie",
        "tmdb_id": tmdb_id,
        "title": meta.get("title") or title,
        "streams": streams,
    }


@router.get("/mw/watch/{tmdb_id}/{season_number}/{episode_number}")
@limiter.limit("30/minute")
async def mw_watch_tv(request: Request, tmdb_id: int, season_number: int, episode_number: int):
    anilist_id, title = await tv_start(tmdb_id, season_number)
    meta, streams = await _collect(
        watch_events(
            tmdb_id,
            season_number,
            episode_number,
            anilist_id,
            title,
            base_url=public_base_url(request),
        )
    )
    payload = {
        "success": True,
        "media": "tv",
        "tmdb_id": tmdb_id,
        "season": season_number,
        "episode": episode_number,
        "title": meta.get("title") or title,
        "streams": streams,
    }
    if meta.get("unaired"):
        payload["unaired"] = True
        payload["air_date"] = meta.get("air_date")
    return payload
