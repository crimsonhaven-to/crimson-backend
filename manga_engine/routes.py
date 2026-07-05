"""Manga API — discovery (AniList), overview, the chapter page manifest, and the
optional same-origin image relay.

The public backend is a **metadata + orchestration** layer only: it never talks to
a manga host. Discovery is pure AniList (like TMDB for video); the chapter list and
page images are resolved in the viewer's browser by ``crimson-sources`` (E2/E3) and
merged client-side. So in a base build ``/manga-overview`` returns the AniList
metadata with an *empty* chapter list (``mapped: false``) plus the candidate titles
and preference config the client needs to resolve them itself, and ``/read`` /
``/manga_proxy`` are dormant.

An operator build may inject a private ``MangaProvider`` (see ``provider.py``); when
present the same routes fill the chapters/pages server-side (extension-less devices
then read without any client offload), and ``/manga_proxy`` relays the page images
same-origin (public + HMAC-signed, the reading twin of ``/subtitles_proxy``). The
AniList→id mapping and chapter list are cached through ``core.response_cache`` (no DB
table), so a base deploy needs no migration.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import Response, StreamingResponse

from core.http_client import http_client
from core.response_cache import get_cached_response, set_cached_response
from metadata_engine.anilist import (
    MANGA_DEFAULT_SORT,
    fetch_anilist_genres,
    fetch_anilist_manga_metadata,
    fetch_manga_catalogue,
    fetch_trending_manga,
    search_anilist_manga,
)
from web.util import _public_base_url

from .provider import (
    content_ratings,
    default_language,
    get_provider,
    manga_enabled,
    preferred_languages,
)

logger = logging.getLogger("crimson.manga.routes")

router = APIRouter(tags=["manga"])

# The mapping rarely changes, so cache a found id for a week; misses are NOT cached
# so a title that gains a match later is picked up on the next open.
_MAP_TTL_SECONDS = 7 * 24 * 3600
# Chapter lists change as scanlations release; a few hours keeps re-opens cheap.
_CHAPTERS_TTL_SECONDS = 6 * 3600


def _candidate_titles(meta: dict) -> List[str]:
    """AniList titles + synonyms in match-priority order (romaji tends to match
    best, then english/native, then synonyms). Also handed to the client so its
    own resolution uses the exact same candidate set."""
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
    """AniList entry → provider manga id, memoized in the response cache. Only ever
    called when a provider is present (base builds skip this entirely)."""
    anilist_id = meta.get("anilist_id")
    if not anilist_id:
        return None
    cache_key = f"manga:map:{anilist_id}"
    cached = await get_cached_response(cache_key)
    if cached and cached.get("mangadex_id"):
        return cached["mangadex_id"]
    manga_id = await provider.resolve_manga_id(_candidate_titles(meta))
    if manga_id:
        await set_cached_response(cache_key, {"mangadex_id": manga_id}, ttl_seconds=_MAP_TTL_SECONDS)
    return manga_id


async def _get_chapters_cached(provider, manga_id: str, language: str) -> List[dict]:
    cache_key = f"manga:chapters:{manga_id}:{language}"
    cached = await get_cached_response(cache_key)
    if cached and "chapters" in cached:
        return cached["chapters"]
    chapters = await provider.get_chapters(manga_id, language)
    if chapters:
        await set_cached_response(cache_key, {"chapters": chapters}, ttl_seconds=_CHAPTERS_TTL_SECONDS)
    return chapters


# --- discovery --------------------------------------------------------------
@router.get("/search/manga")
async def search_manga(query_name: str = Query(..., min_length=1, description="Manga name to search")):
    """Search manga by name via AniList (kind='manga', keyed by anilist_id)."""
    if not manga_enabled():
        raise HTTPException(status_code=503, detail="Manga is not enabled")
    async with http_client() as client:
        results = await search_anilist_manga(client, query_name)
    return {"success": True, "query": query_name, "count": len(results), "suggestions": results}


@router.get("/trending/manga")
async def trending_manga(limit: int = Query(12, ge=1, le=50, description="Number of results to return")):
    """Trending manga via AniList."""
    if not manga_enabled():
        raise HTTPException(status_code=503, detail="Manga is not enabled")
    async with http_client() as client:
        results = await fetch_trending_manga(client, limit)
    return {"success": True, "count": len(results), "manga": results}


@router.get("/catalogue/manga")
async def catalogue_manga(
    genre: Optional[str] = Query(None, description="Optional AniList genre filter, e.g. Action, Romance"),
    sort: str = Query(MANGA_DEFAULT_SORT, description="trending | popular | score | newest | title"),
    page: int = Query(1, ge=1, le=200, description="1-based page for the browse hub"),
):
    """The Manga browse hub — one page of AniList MANGA, filterable by genre and
    sortable. Unlike /catalogue/shows|movies this is live + paginated (there is no
    local manga table), so the frontend appends pages via ``has_next``. ``genres``
    is AniList's shared genre vocabulary for the filter chips.
    """
    if not manga_enabled():
        raise HTTPException(status_code=503, detail="Manga is not enabled")
    async with http_client() as client:
        genres = await fetch_anilist_genres(client)
        result = await fetch_manga_catalogue(client, genre=genre, sort=sort, page=page)
    # Upstream (AniList) failure → 503, so the hub shows "temporarily unavailable"
    # rather than a misleading empty grid (AniList returns HTTP 200 even on outage).
    if result.get("unavailable"):
        raise HTTPException(status_code=503, detail="Manga discovery (AniList) is temporarily unavailable — please try again shortly.")
    return {
        "success": True,
        "count": len(result["items"]),
        "total": result.get("total", 0),
        "page": result.get("page", page),
        "has_next": result.get("has_next", False),
        "sort": sort,
        # Shape parity with the anime/local genre facet ([{genre, count}]); manga
        # has no per-genre counts (live corpus), so count is omitted.
        "genres": [{"genre": g} for g in genres],
        "manga": result["items"],
    }


# --- overview (metadata + chapter list) ------------------------------------
@router.get("/manga-overview/{anilist_id}")
async def manga_overview(
    anilist_id: int,
    language: Optional[str] = Query(None, description="Preferred chapter language (default: server default)"),
):
    """AniList manga metadata plus — when a server-side provider is present — the
    mapped chapter list. In a base build ``chapters`` is empty (``mapped`` false) and
    the client resolves the chapter list itself from ``candidate_titles`` +
    ``content_rating`` + ``language`` (all returned here), so the page always renders.
    """
    if not manga_enabled():
        raise HTTPException(status_code=503, detail="Manga is not enabled")
    async with http_client() as client:
        meta = await fetch_anilist_manga_metadata(client, anilist_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Manga not found on AniList")

    lang = language or default_language()
    provider = get_provider()

    manga_id: Optional[str] = None
    chapters: List[dict] = []
    if provider is not None and provider.configured():
        manga_id = await _resolve_manga_id(provider, meta)
        chapters = await _get_chapters_cached(provider, manga_id, lang) if manga_id else []

    return {
        "success": True,
        **meta,
        # The client uses these to resolve chapters itself when the backend didn't
        # (base build). Names no host — just AniList titles + preference config.
        "candidate_titles": _candidate_titles(meta),
        "content_rating": content_ratings(),
        "languages": preferred_languages(),
        "mangadex_id": manga_id,
        "mapped": bool(manga_id),
        "language": lang,
        "chapter_count": len(chapters),
        "chapters": chapters,
    }


# --- chapter page manifest (the reading twin of /watch) --------------------
@router.get("/read/{anilist_id}/{chapter_id}")
async def read_chapter(
    request: Request,
    anilist_id: int,
    chapter_id: str,
    data_saver: bool = Query(False, description="Serve smaller data-saver images"),
):
    """Ordered page images for one chapter, when a server-side provider is present.
    In a base build there is no provider, so this reports 404 and the client resolves
    the pages itself (raw image URLs an ``<img>`` loads directly — no proxy needed)."""
    if not manga_enabled():
        raise HTTPException(status_code=503, detail="Manga is not enabled")
    provider = get_provider()
    if provider is None or not provider.configured():
        # No server-side source: the browser (crimson-sources) resolves this chapter.
        raise HTTPException(status_code=404, detail="Resolved client-side")
    base_url = _public_base_url(request)
    pages = await provider.get_chapter_pages(chapter_id, base_url=base_url, data_saver=data_saver)
    if not pages:
        raise HTTPException(status_code=404, detail="Chapter pages unavailable")
    return {
        "success": True,
        "anilist_id": anilist_id,
        "chapter_id": chapter_id,
        "count": len(pages),
        "pages": pages,
    }


# --- optional same-origin image relay (present only with a provider) -------
@router.get("/manga_proxy")
async def manga_proxy(
    u: str = Query(..., description="Upstream page-image URL (HMAC-signed)"),
    s: str = Query(..., description="signature"),
):
    """Relay one page image same-origin. Only functional in a provider (operator)
    build — the provider owns the HMAC verification AND host allow-list, so it can't
    be turned into an open proxy. Dormant (503) in a base build, where page images are
    served to the browser as raw upstream URLs instead."""
    provider = get_provider()
    if not manga_enabled() or provider is None or not provider.configured():
        raise HTTPException(status_code=503, detail="Manga relay not available")
    try:
        status, content_type, headers, payload = await provider.proxy_fetch(u, s)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:  # upstream/network trouble
        logger.warning(f"[manga_proxy] fetch failed: {type(e).__name__} - {e}")
        raise HTTPException(status_code=502, detail="Image unavailable")
    if isinstance(payload, (bytes, bytearray)):
        return Response(content=payload, status_code=status, media_type=content_type, headers=headers)
    return StreamingResponse(payload, status_code=status, media_type=content_type, headers=headers)
