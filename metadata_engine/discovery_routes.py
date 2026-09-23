"""Search, trending and the browse catalogues for anime, TV shows and movies."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core import json_response
from core.config import Settings, get_settings
from core.http_client import http_client

from . import browse
from .anilist import CATALOGUE_DEFAULT_SORT, fetch_anilist_genres, fetch_anime_catalogue
from .search import search_anime
from .tmdb import (
    fetch_tmdb_movie_search_results,
    fetch_tmdb_show_search_results,
    fetch_trending_anime,
    fetch_trending_movies,
    fetch_trending_shows,
)

logger = logging.getLogger("crimson.discovery")

router = APIRouter(tags=["discovery"])


def _require_tmdb(settings: Settings = Depends(get_settings)) -> None:
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")


async def _upstream(fetch, error: str):
    """TMDB failures surface as a 500 with a readable message, not a traceback."""
    try:
        async with http_client() as client:
            return await fetch(client)
    except Exception as e:
        logger.error(f"{error}: {e}")
        raise HTTPException(status_code=500, detail=error)


@router.get("/search/anime")
async def search_anime_by_name(
    query_name: str = Query(..., min_length=1, description="Anime name to search"),
    settings: Settings = Depends(get_settings),
):
    try:
        results = await search_anime(query_name, tmdb_enabled=bool(settings.tmdb_api_key))
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")
    return {"success": True, "query": query_name, "count": len(results), "suggestions": results}


@router.get("/trending")
async def get_trending_anime(
    limit: int = Query(10, ge=1, le=50, description="Number of results to return"),
):
    results = await _upstream(
        lambda c: fetch_trending_anime(c, limit), "Failed to fetch trending anime"
    )
    return {"success": True, "count": len(results), "animes": results}


@router.get("/search/shows", dependencies=[Depends(_require_tmdb)])
async def search_shows_by_name(
    query_name: str = Query(..., min_length=1, description="TV show name to search"),
):
    results = await _upstream(
        lambda c: fetch_tmdb_show_search_results(c, query_name), "Search failed"
    )
    return {"success": True, "query": query_name, "count": len(results), "suggestions": results}


@router.get("/trending/shows")
async def get_trending_shows(
    limit: int = Query(10, ge=1, le=50, description="Number of results to return"),
):
    results = await _upstream(
        lambda c: fetch_trending_shows(c, limit), "Failed to fetch trending shows"
    )
    return {"success": True, "count": len(results), "shows": results}


@router.get("/search/movies", dependencies=[Depends(_require_tmdb)])
async def search_movies_by_name(
    query_name: str = Query(..., min_length=1, description="Movie name to search"),
):
    results = await _upstream(
        lambda c: fetch_tmdb_movie_search_results(c, query_name), "Search failed"
    )
    return {"success": True, "query": query_name, "count": len(results), "suggestions": results}


@router.get("/trending/movies")
async def get_trending_movies(
    limit: int = Query(10, ge=1, le=50, description="Number of results to return"),
):
    results = await _upstream(
        lambda c: fetch_trending_movies(c, limit), "Failed to fetch trending movies"
    )
    return {"success": True, "count": len(results), "movies": results}


def _gzipped(request: Request, result):
    if isinstance(result, dict):
        return json_response.gzip_json(request, result)
    return json_response.respond(request, result)


@router.get("/catalogue")
async def get_catalogue(
    request: Request,
    category: Optional[str] = Query(
        None, description="Optional format filter, e.g. TV, MOVIE, OVA, ONA, SPECIAL"
    ),
    genre: Optional[str] = Query(
        None, description="Optional genre filter, e.g. Action, Romance, Comedy"
    ),
):
    """The full mapped anime archive from the local database, gzipped."""
    return _gzipped(request, await browse.anime_catalogue(category, genre))


@router.get("/catalogue/anime")
async def get_anime_catalogue(
    genre: Optional[str] = Query(
        None, description="Optional AniList genre filter, e.g. Action, Romance"
    ),
    sort: str = Query(
        CATALOGUE_DEFAULT_SORT, description="trending | popular | score | newest | title"
    ),
    page: int = Query(1, ge=1, le=200, description="1-based page for the browse hub"),
):
    """One page of AniList anime, the hub's fast default view. When AniList is
    down and no cached page exists, the local archive answers instead and
    ``fallback`` is true."""
    async with http_client() as client:
        genres = await fetch_anilist_genres(client)
        result = await fetch_anime_catalogue(client, genre=genre, sort=sort, page=page)
        if result.get("unavailable"):
            fb = await browse.local_anime_fallback(client, genre=genre, sort=sort, page=page)
            return {
                "success": True,
                "count": len(fb["items"]),
                "total": fb["total"],
                "page": fb["page"],
                "has_next": fb["has_next"],
                "sort": sort,
                "fallback": True,
                "genres": fb["genres"],
                "animes": fb["items"],
            }
    return {
        "success": True,
        "count": len(result["items"]),
        "total": result.get("total", 0),
        "page": result.get("page", page),
        "has_next": result.get("has_next", False),
        "sort": sort,
        "fallback": False,
        # True when AniList was down but a cached page was served.
        "stale": bool(result.get("stale")),
        "genres": [{"genre": g} for g in genres],
        "animes": result["items"],
    }


@router.get("/catalogue/shows")
async def get_shows_catalogue(
    request: Request,
    genre: Optional[str] = Query(
        None, description="Optional genre filter, e.g. Drama, Comedy, Crime"
    ),
):
    """Non-anime TV from the local tables, popular first."""
    return _gzipped(request, await browse.shows_catalogue(genre))


@router.get("/catalogue/movies")
async def get_movies_catalogue(
    request: Request,
    genre: Optional[str] = Query(
        None, description="Optional genre filter, e.g. Action, Drama, Horror"
    ),
):
    """Movies from the local tables, popular first, with ``vote_average``."""
    return _gzipped(request, await browse.movies_catalogue(genre))
