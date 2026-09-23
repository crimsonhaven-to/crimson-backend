"""Manga discovery, overview, the chapter page manifest and the image relay.

Discovery and metadata are AniList. In a base build the chapter list and pages
resolve in the browser, so ``/manga-overview`` returns an empty chapter list plus
the candidate titles and preferences the client needs, and ``/read`` and
``/manga_proxy`` stay dormant. An injected provider (see ``provider.py``) fills
them server-side instead.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import Response, StreamingResponse

from core.config import Settings, get_settings
from core.http_client import http_client
from core.public_url import public_base_url
from core.response_cache import get_cached_response, set_cached_response
from metadata_engine.anilist import (
    CATALOGUE_DEFAULT_SORT,
    fetch_anilist_genres,
    fetch_anilist_manga_metadata,
    fetch_manga_catalogue,
    fetch_trending_manga,
    search_anilist_manga,
)

from .provider import get_provider

logger = logging.getLogger("crimson.manga.routes")


def require_manga_enabled(settings: Settings = Depends(get_settings)) -> Settings:
    if not settings.manga_enabled:
        raise HTTPException(status_code=503, detail="Manga is not enabled")
    return settings


router = APIRouter(tags=["manga"])

# A found id rarely changes. Misses are not cached, so a title that gains a match
# later is picked up on the next open.
_MAP_TTL_SECONDS = 7 * 24 * 3600
_CHAPTERS_TTL_SECONDS = 6 * 3600


def _candidate_titles(meta: dict) -> List[str]:
    """AniList titles and synonyms in match-priority order, deduplicated. The client
    gets the same list, so its own resolution matches a provider build's."""
    titles = [
        meta.get("title_romaji"),
        meta.get("title_english"),
        meta.get("title"),
        meta.get("title_native"),
    ]
    titles += meta.get("synonyms") or []
    seen, out = set(), []
    for t in titles:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


async def _resolve_manga_id(provider, meta: dict) -> Optional[str]:
    anilist_id = meta.get("anilist_id")
    if not anilist_id:
        return None
    cache_key = f"manga:map:{anilist_id}"
    cached = await get_cached_response(cache_key)
    if cached and cached.get("mangadex_id"):
        return cached["mangadex_id"]
    manga_id = await provider.resolve_manga_id(_candidate_titles(meta))
    if manga_id:
        await set_cached_response(
            cache_key, {"mangadex_id": manga_id}, ttl_seconds=_MAP_TTL_SECONDS
        )
    return manga_id


async def _get_chapters_cached(provider, manga_id: str, language: str) -> List[dict]:
    cache_key = f"manga:chapters:{manga_id}:{language}"
    cached = await get_cached_response(cache_key)
    if cached and "chapters" in cached:
        return cached["chapters"]
    chapters = await provider.get_chapters(manga_id, language)
    if chapters:
        await set_cached_response(
            cache_key, {"chapters": chapters}, ttl_seconds=_CHAPTERS_TTL_SECONDS
        )
    return chapters


@router.get("/search/manga", dependencies=[Depends(require_manga_enabled)])
async def search_manga(
    query_name: str = Query(..., min_length=1, description="Manga name to search"),
):
    async with http_client() as client:
        results = await search_anilist_manga(client, query_name)
    return {"success": True, "query": query_name, "count": len(results), "suggestions": results}


@router.get("/trending/manga", dependencies=[Depends(require_manga_enabled)])
async def trending_manga(
    limit: int = Query(12, ge=1, le=50, description="Number of results to return"),
):
    """``stale`` is true when AniList was down and the last good row was served."""
    async with http_client() as client:
        result = await fetch_trending_manga(client, limit)
    items = result["items"]
    return {"success": True, "count": len(items), "stale": result["stale"], "manga": items}


@router.get("/catalogue/manga", dependencies=[Depends(require_manga_enabled)])
async def catalogue_manga(
    genre: Optional[str] = Query(
        None, description="Optional AniList genre filter, e.g. Action, Romance"
    ),
    sort: str = Query(
        CATALOGUE_DEFAULT_SORT, description="trending | popular | score | newest | title"
    ),
    page: int = Query(1, ge=1, le=200, description="1-based page for the browse hub"),
):
    """One live page of AniList manga. There is no local manga table, so unlike
    /catalogue/shows this paginates upstream and the client appends on ``has_next``."""
    async with http_client() as client:
        genres = await fetch_anilist_genres(client)
        result = await fetch_manga_catalogue(client, genre=genre, sort=sort, page=page)
    # AniList answers 200 even during an outage, so an empty grid would look like
    # "no manga" rather than "try again".
    if result.get("unavailable"):
        raise HTTPException(
            status_code=503,
            detail="Manga discovery (AniList) is temporarily unavailable. Please try again shortly.",
        )
    return {
        "success": True,
        "count": len(result["items"]),
        "total": result.get("total", 0),
        "page": result.get("page", page),
        "has_next": result.get("has_next", False),
        "sort": sort,
        "stale": bool(result.get("stale")),
        # Same shape as the anime genre facet, minus counts: the corpus is live.
        "genres": [{"genre": g} for g in genres],
        "manga": result["items"],
    }


@router.get("/manga-overview/{anilist_id}")
async def manga_overview(
    anilist_id: int,
    language: Optional[str] = Query(
        None, description="Preferred chapter language (default: server default)"
    ),
    settings: Settings = Depends(require_manga_enabled),
):
    """AniList metadata, plus the chapter list when a provider is present. Without
    one the client resolves chapters from ``candidate_titles``, ``content_rating``
    and ``language``."""
    async with http_client() as client:
        meta = await fetch_anilist_manga_metadata(client, anilist_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Manga not found on AniList")

    lang = language or (settings.manga_languages or ["en"])[0]
    provider = get_provider()

    manga_id: Optional[str] = None
    chapters: List[dict] = []
    if provider is not None and provider.configured():
        manga_id = await _resolve_manga_id(provider, meta)
        chapters = await _get_chapters_cached(provider, manga_id, lang) if manga_id else []

    return {
        "success": True,
        **meta,
        "candidate_titles": _candidate_titles(meta),
        "content_rating": settings.manga_content_rating,
        "languages": settings.manga_languages,
        "mangadex_id": manga_id,
        "mapped": bool(manga_id),
        "language": lang,
        "chapter_count": len(chapters),
        "chapters": chapters,
    }


@router.get("/read/{anilist_id}/{chapter_id}", dependencies=[Depends(require_manga_enabled)])
async def read_chapter(
    request: Request,
    anilist_id: int,
    chapter_id: str,
    data_saver: bool = Query(False, description="Serve smaller data-saver images"),
):
    """Ordered page images for one chapter. Without a provider this is a 404 and
    the browser resolves the pages itself."""
    provider = get_provider()
    if provider is None or not provider.configured():
        raise HTTPException(status_code=404, detail="Resolved client-side")
    pages = await provider.get_chapter_pages(
        chapter_id, base_url=public_base_url(request), data_saver=data_saver
    )
    if not pages:
        raise HTTPException(status_code=404, detail="Chapter pages unavailable")
    return {
        "success": True,
        "anilist_id": anilist_id,
        "chapter_id": chapter_id,
        "count": len(pages),
        "pages": pages,
    }


@router.get("/manga_proxy")
async def manga_proxy(
    u: str = Query(..., description="Upstream page-image URL (HMAC-signed)"),
    s: str = Query(..., description="signature"),
    settings: Settings = Depends(get_settings),
):
    """Relay one page image same-origin. The provider owns the HMAC check and the
    host allow-list, so this cannot become an open proxy."""
    provider = get_provider()
    if not settings.manga_enabled or provider is None or not provider.configured():
        raise HTTPException(status_code=503, detail="Manga relay not available")
    try:
        status, content_type, headers, payload = await provider.proxy_fetch(u, s)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.warning(f"[manga_proxy] fetch failed: {type(e).__name__} - {e}")
        raise HTTPException(status_code=502, detail="Image unavailable")
    if isinstance(payload, (bytes, bytearray)):
        return Response(
            content=payload, status_code=status, media_type=content_type, headers=headers
        )
    return StreamingResponse(payload, status_code=status, media_type=content_type, headers=headers)
