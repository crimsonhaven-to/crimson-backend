"""Discovery endpoints: per-surface search + trending, and the anime catalogue.

Anime (/search/anime, /trending), non-anime TV shows (/search/shows,
/trending/shows) and general movies (/search/movies, /trending/movies) each get a
search + trending pair, all TMDB-keyed; /catalogue lists the full mapped anime
library from the local DB with no external calls. Lifted verbatim from ``api.py``.
"""

import asyncio
import logging
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request

from core.config import Config
from core.http_client import http_client
from core.response_cache import (
    _local_cache,
    _local_get,
    _local_set,
    get_cached_response,
    set_cached_response,
)
from metadata_engine.anilist import (
    CATALOGUE_DEFAULT_SORT,
    fetch_anilist_genres,
    fetch_anime_catalogue,
)
from metadata_engine.tmdb import (
    fetch_tmdb_search_results,
    fetch_trending_anime,
    fetch_tmdb_show_search_results,
    fetch_trending_shows,
    fetch_tmdb_movie_search_results,
    fetch_trending_movies,
)

from web.queries import (
    get_catalogue_items,
    get_movies_catalogue_items,
    get_shows_catalogue_items,
)
from web.serialization import _gzip_json, _gzip_response, _json_gzip_bodies

logger = logging.getLogger("crimson.discovery")

router = APIRouter()


@router.get("/search/anime")
async def search_anime_by_name(query_name: str = Query(..., min_length=1, description="Anime name to search")):
    """Search for anime by name"""
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")

    try:
        async with http_client() as client:
            results = await fetch_tmdb_search_results(client, query_name)

        return {
            "success": True,
            "query": query_name,
            "count": len(results),
            "suggestions": results
        }
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/trending")
async def get_trending_anime(limit: int = Query(10, ge=1, le=50, description="Number of results to return")):
    """Get trending anime"""
    try:
        async with http_client() as client:
            results = await fetch_trending_anime(client, limit)

        return {
            "success": True,
            "count": len(results),
            "animes": results
        }
    except Exception as e:
        logger.error(f"Trending error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trending anime")


# --- Non-anime TV shows (secondary surface) ---------------------------------
# Parallel to /search/anime + /trending, but for general TV shows. They reuse the
# existing TMDB-keyed playback path (/info + /watch/{tmdb_id}/{season}/{episode}),
# so no new watch/info routes are needed — only discovery + a TMDB-keyed overview.

@router.get("/search/shows")
async def search_shows_by_name(query_name: str = Query(..., min_length=1, description="TV show name to search")):
    """Search for non-anime TV shows by name (kind='show', keyed by tmdb_id)."""
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")
    try:
        async with http_client() as client:
            results = await fetch_tmdb_show_search_results(client, query_name)
        return {
            "success": True,
            "query": query_name,
            "count": len(results),
            "suggestions": results,
        }
    except Exception as e:
        logger.error(f"Show search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/trending/shows")
async def get_trending_shows(limit: int = Query(10, ge=1, le=50, description="Number of results to return")):
    """Get trending non-anime TV shows."""
    try:
        async with http_client() as client:
            results = await fetch_trending_shows(client, limit)
        return {
            "success": True,
            "count": len(results),
            "shows": results,
        }
    except Exception as e:
        logger.error(f"Trending shows error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trending shows")


# --- General (non-anime) movies (secondary surface) -------------------------
# Parallel to /search/shows + /trending/shows, but for standalone movies. They are
# played by /watch/movie/{tmdb_id} (movies have no season/episode) and described by
# /movie-overview/{tmdb_id}.

@router.get("/search/movies")
async def search_movies_by_name(query_name: str = Query(..., min_length=1, description="Movie name to search")):
    """Search for general movies by name (kind='movie', keyed by tmdb_id)."""
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")
    try:
        async with http_client() as client:
            results = await fetch_tmdb_movie_search_results(client, query_name)
        return {
            "success": True,
            "query": query_name,
            "count": len(results),
            "suggestions": results,
        }
    except Exception as e:
        logger.error(f"Movie search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/trending/movies")
async def get_trending_movies(limit: int = Query(10, ge=1, le=50, description="Number of results to return")):
    """Get trending general movies."""
    try:
        async with http_client() as client:
            results = await fetch_trending_movies(client, limit)
        return {
            "success": True,
            "count": len(results),
            "movies": results,
        }
    except Exception as e:
        logger.error(f"Trending movies error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trending movies")


@router.get("/catalogue")
async def get_catalogue(
    request: Request,
    category: Optional[str] = Query(None, description="Optional format filter, e.g. TV, MOVIE, OVA, ONA, SPECIAL"),
    genre: Optional[str] = Query(None, description="Optional genre filter, e.g. Action, Romance, Comedy"),
):
    """Full anime catalogue for a 'browse by category' page.

    Lists every anime in our local DB (name + format + genres + navigation ids)
    with no external API calls. ``categories`` and ``genres`` always reflect the
    whole catalogue (so the frontend can render all tabs/chips); ``animes`` is
    filtered when a ``category`` (format) and/or ``genre`` query param is given.

    The (large) response is gzip-compressed when the client accepts it.
    """
    # v2: item shape gained a `genres` field; bump so pre-genre cached lists
    # (which lack it) aren't served.
    cache_key = "catalogue:v2"
    derived_key = "catalogue:v2:derived"  # memoized full-catalogue breakdowns
    body_key = "catalogue:v2:body"        # memoized unfiltered response bodies
    # L1: in-process cache for the whole item list (no DB round-trip on a hit).
    items = _local_get(cache_key)
    derived = _local_get(derived_key)
    if items is None or derived is None:
        if items is None:
            cached = await get_cached_response(cache_key)
            if cached and "items" in cached:
                items = cached["items"]
            else:
                loop = asyncio.get_event_loop()
                items = await loop.run_in_executor(None, get_catalogue_items)
                if items:
                    await set_cached_response(cache_key, {"items": items}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
            items = items or []
            _local_set(cache_key, items)

        # Format + genre breakdowns over the FULL catalogue (before any filtering).
        # They only change when `items` is reloaded, so memoize them rather than
        # re-scanning the whole list on every request (the previous behaviour, paid
        # even on a cache hit). Recomputed whenever either L1 slot expired.
        counts: Dict[str, int] = {}
        genre_counts: Dict[str, int] = {}
        for it in items:
            counts[it["category"]] = counts.get(it["category"], 0) + 1
            for g in it.get("genres") or []:
                genre_counts[g] = genre_counts.get(g, 0) + 1
        derived = {
            "categories": [{"category": k, "count": v} for k, v in sorted(counts.items())],
            "genres": [{"genre": k, "count": v} for k, v in sorted(genre_counts.items())],
        }
        _local_set(derived_key, derived)
        # A fresh item list invalidates any cached unfiltered body.
        _local_cache.pop(body_key, None)

    # Unfiltered catalogue is by far the most-requested shape and is identical for
    # every client within a cache window — serialize + gzip it once and reuse the
    # bytes (orjson encode + level-6 gzip of the full list was the real per-request
    # cost). Filtered views (category/genre) are smaller and computed on demand.
    if not category and not genre:
        bodies = _local_get(body_key)
        if bodies is None:
            bodies = _json_gzip_bodies({
                "success": True,
                "count": len(items),
                "total": len(items),
                "categories": derived["categories"],
                "genres": derived["genres"],
                "animes": items,
            })
            _local_set(body_key, bodies)
        return _gzip_response(request, bodies)

    animes = items
    if category:
        wanted = category.strip().upper()
        animes = [it for it in animes if (it["category"] or "").upper() == wanted]
    if genre:
        wanted_g = genre.strip().casefold()
        animes = [
            it for it in animes
            if any(g.casefold() == wanted_g for g in (it.get("genres") or []))
        ]

    return _gzip_json(request, {
        "success": True,
        "count": len(animes),
        "total": len(items),
        "categories": derived["categories"],
        "genres": derived["genres"],
        "animes": animes,
    })


@router.get("/catalogue/anime")
async def get_anime_catalogue(
    genre: Optional[str] = Query(None, description="Optional AniList genre filter, e.g. Action, Romance"),
    sort: str = Query(CATALOGUE_DEFAULT_SORT, description="trending | popular | score | newest | title"),
    page: int = Query(1, ge=1, le=200, description="1-based page for the browse hub"),
):
    """The fast, default anime browse — one page of AniList ANIME (poster-rich,
    genre + sort, paginated), the anime twin of /catalogue/manga. Distinct from
    /catalogue (the full 6,800-title local archive, which the hub keeps as a
    secondary view): shipping + rendering that whole list is slow, so this is the
    landing view. Items are ``kind: 'anime'`` cards keyed by anilist_id.
    """
    async with http_client() as client:
        genres = await fetch_anilist_genres(client)
        result = await fetch_anime_catalogue(client, genre=genre, sort=sort, page=page)
    # Upstream (AniList) failure — surface it as 503 so the hub shows an honest
    # "temporarily unavailable" instead of a misleading empty grid. The anime hub's
    # local "Archive" view is unaffected.
    if result.get("unavailable"):
        raise HTTPException(status_code=503, detail="Anime discovery (AniList) is temporarily unavailable — please try again shortly.")
    return {
        "success": True,
        "count": len(result["items"]),
        "total": result.get("total", 0),
        "page": result.get("page", page),
        "has_next": result.get("has_next", False),
        "sort": sort,
        "genres": [{"genre": g} for g in genres],
        "animes": result["items"],
    }


# --- Non-anime catalogues (shows / movies), served from the local TMDB tables ---
# The show/movie twins of /catalogue: a full browse list built entirely from the
# local tmdb_shows / tmdb_movies tables (no live TMDB), with a genre facet the
# frontend renders as filter chips. Same three-cache + gzip treatment as
# /catalogue; genres always reflect the WHOLE catalogue so every chip renders,
# while the list is filtered when a ?genre= is given. Kept generic because shows
# and movies differ only in table/list-key (movies additionally carry
# vote_average, already baked into the item by the builder).

async def _serve_local_catalogue(request, *, cache_prefix, builder, list_key, genre):
    """Shared /catalogue-style responder for a locally-built poster-card list.

    ``builder`` is the sync DB reader (run in a threadpool). ``list_key`` names
    the item array in the JSON body (``shows`` / ``movies``). The unfiltered body
    is serialized + gzipped once per cache window and the bytes reused; a
    ``genre`` filter is computed on demand. Mirrors get_catalogue's cache layout.
    """
    items_key = f"{cache_prefix}:v1"
    derived_key = f"{cache_prefix}:v1:derived"
    body_key = f"{cache_prefix}:v1:body"

    items = _local_get(items_key)
    derived = _local_get(derived_key)
    if items is None or derived is None:
        if items is None:
            cached = await get_cached_response(items_key)
            if cached and "items" in cached:
                items = cached["items"]
            else:
                loop = asyncio.get_event_loop()
                items = await loop.run_in_executor(None, builder)
                if items:
                    await set_cached_response(items_key, {"items": items}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
            items = items or []
            _local_set(items_key, items)

        # Genre breakdown over the FULL catalogue (before any filtering), memoized
        # like the anime catalogue's — shows/movies have no format "category" axis.
        genre_counts: Dict[str, int] = {}
        for it in items:
            for g in it.get("genres") or []:
                genre_counts[g] = genre_counts.get(g, 0) + 1
        derived = {"genres": [{"genre": k, "count": v} for k, v in sorted(genre_counts.items())]}
        _local_set(derived_key, derived)
        _local_cache.pop(body_key, None)

    if not genre:
        bodies = _local_get(body_key)
        if bodies is None:
            bodies = _json_gzip_bodies({
                "success": True,
                "count": len(items),
                "total": len(items),
                "genres": derived["genres"],
                list_key: items,
            })
            _local_set(body_key, bodies)
        return _gzip_response(request, bodies)

    wanted_g = genre.strip().casefold()
    filtered = [
        it for it in items
        if any(g.casefold() == wanted_g for g in (it.get("genres") or []))
    ]
    return _gzip_json(request, {
        "success": True,
        "count": len(filtered),
        "total": len(items),
        "genres": derived["genres"],
        list_key: filtered,
    })


@router.get("/catalogue/shows")
async def get_shows_catalogue(
    request: Request,
    genre: Optional[str] = Query(None, description="Optional genre filter, e.g. Drama, Comedy, Crime"),
):
    """Full non-anime TV-show catalogue for the Shows browse hub (local DB only).

    Items are ``kind: 'show'`` poster cards keyed by tmdb_id, ordered popular-first.
    ``genres`` always reflects the whole catalogue; ``shows`` is filtered by
    ``genre`` when given. Gzip-compressed when the client accepts it.
    """
    return await _serve_local_catalogue(
        request, cache_prefix="catalogue:shows", builder=get_shows_catalogue_items,
        list_key="shows", genre=genre,
    )


@router.get("/catalogue/movies")
async def get_movies_catalogue(
    request: Request,
    genre: Optional[str] = Query(None, description="Optional genre filter, e.g. Action, Drama, Horror"),
):
    """Full general-movie catalogue for the Movies browse hub (local DB only).

    Items are ``kind: 'movie'`` poster cards keyed by tmdb_id (carrying
    ``vote_average``), ordered popular-first. Genre facet + filter as /catalogue/shows.
    """
    return await _serve_local_catalogue(
        request, cache_prefix="catalogue:movies", builder=get_movies_catalogue_items,
        list_key="movies", genre=genre,
    )
