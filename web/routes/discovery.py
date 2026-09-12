"""Discovery: per-surface search and trending, plus the anime catalogue.

Anime, non-anime TV shows and general movies each get a search and trending pair,
all TMDB-keyed. /catalogue lists the full mapped anime library from the local DB
with no external calls.
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
    search_anime_entries,
)
from web.serialization import _gzip_json, _gzip_response, _json_gzip_bodies

logger = logging.getLogger("crimson.discovery")

router = APIRouter()


# Below this many local hits the TMDB search runs too, so a title added upstream
# since the last Fribb sync still resolves. Above it, the local catalogue already
# holds everything TMDB would have offered, since fetch_tmdb_search_results drops
# every TMDB result that has no local AniList mapping anyway.
_LOCAL_SEARCH_FLOOR = 3


@router.get("/search/anime")
async def search_anime_by_name(query_name: str = Query(..., min_length=1, description="Anime name to search")):
    """Search anime by name, from the local catalogue first.

    The client fires a search per keystroke across five surfaces, so the cost
    that matters is the round trip, not the query. anime_entries already holds
    the whole mapped catalogue, and TMDB results without a mapping are discarded
    downstream regardless, so the local table answers most searches outright and
    TMDB is consulted only when it returns few enough hits to be worth it.
    """
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")

    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: search_anime_entries(query_name)
        )

        if len(results) < _LOCAL_SEARCH_FLOOR:
            async with http_client() as client:
                remote = await fetch_tmdb_search_results(client, query_name)
            seen = {r["anilist_id"] for r in results}
            # Local first: its rows are ranked against the query, where TMDB's
            # order reflects TMDB's own popularity.
            results = results + [r for r in remote if r["anilist_id"] not in seen]

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
    """Trending anime."""
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
# The general-TV twins of /search/anime and /trending. They reuse the TMDB-keyed
# playback path, so only discovery and an overview are needed here.

@router.get("/search/shows")
async def search_shows_by_name(query_name: str = Query(..., min_length=1, description="TV show name to search")):
    """Search non-anime TV shows by name; ``kind='show'``, keyed by tmdb_id."""
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
    """Trending non-anime TV shows."""
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
# The standalone-movie twins of the show surface, played by /watch/movie/{tmdb_id}
# since a movie has no season or episode.

@router.get("/search/movies")
async def search_movies_by_name(query_name: str = Query(..., min_length=1, description="Movie name to search")):
    """Search general movies by name; ``kind='movie'``, keyed by tmdb_id."""
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
    """Trending general movies."""
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
    """The full anime catalogue, for a browse-by-category page.

    Every anime in the local DB, with no external calls. ``categories`` and
    ``genres`` always describe the whole catalogue so the frontend can render
    every tab and chip, while ``animes`` is filtered by the query params. Gzipped
    when the client accepts it.
    """
    # v2 added `genres` to the item shape; the bump stops pre-genre cached lists
    # being served.
    cache_key = "catalogue:v2"
    derived_key = "catalogue:v2:derived"  # memoized full-catalogue breakdowns
    body_key = "catalogue:v2:body"        # memoized unfiltered response bodies
    # L1 for the whole item list, so a hit costs no DB round-trip.
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

        # Breakdowns over the whole catalogue, before filtering. They only change
        # when `items` reloads, so memoizing avoids re-scanning the list on every
        # request, which used to be paid even on a cache hit.
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

    # The unfiltered catalogue is the most-requested shape and identical for every
    # client within a cache window, and encoding plus gzipping the full list was
    # the real per-request cost, so the bytes are built once and reused. Filtered
    # views are smaller and computed on demand.
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


# --- Local anime fallback (AniList Discover outage) -------------------------
# With AniList's browse API down, /catalogue/anime falls back to the local mapping
# DB rather than 503-ing, so the Discover hub stays alive. The local list is
# paginated and genre-filterable like the real thing, ordered poster-first, and on
# the default view seeded with TMDB trending so page 1 still leads with current
# posters.

LOCAL_ANIME_PER_PAGE = 30


async def _load_catalogue_items_cached() -> list:
    """The full local anime catalogue, sharing /catalogue's cache slot.

    Loads in the same three steps as get_catalogue's item slot, so the fallback
    rides the existing cache instead of paying a DB scan per outage request.
    """
    cache_key = "catalogue:v2"
    items = _local_get(cache_key)
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
    return items


def _anime_year(it: Dict) -> int:
    """Numeric start year for ordering, 0 when unknown."""
    y = it.get("year")
    return int(y) if str(y or "").isdigit() else 0


def _order_local_anime(items: list, sort: str) -> list:
    """Order the local catalogue for the fallback grid. There is no local score, so
    non-title sorts put poster-bearing, newest titles first as a trending stand-in,
    while title sorts stay alphabetical."""
    if sort == "title":
        return sorted(items, key=lambda it: (it.get("title") or "").lower())
    if sort == "newest":
        return sorted(items, key=lambda it: (-_anime_year(it), (it.get("title") or "").lower()))
    # trending, popular and score all mean poster first, newest, then title.
    return sorted(
        items,
        key=lambda it: (0 if it.get("poster") else 1, -_anime_year(it), (it.get("title") or "").lower()),
    )


async def _build_local_anime_fallback(client, *, genre, sort, page, per_page=LOCAL_ANIME_PER_PAGE):
    """One page of the local-DB fallback, in the /catalogue/anime shape.

    Items are ``kind:'anime'`` cards keyed by anilist_id, route-compatible with the
    live grid, and the genre facet covers the whole local catalogue so every chip
    renders. On the default view TMDB trending anime come first and lend posters to
    posterless local twins; a simultaneous TMDB outage just drops that top-up.
    """
    items = await _load_catalogue_items_cached()
    cards = [dict(it, kind="anime") for it in items if it.get("anilist_id")]

    # Over the full catalogue, before filtering, like get_catalogue.
    genre_counts: Dict[str, int] = {}
    for it in cards:
        for g in it.get("genres") or []:
            genre_counts[g] = genre_counts.get(g, 0) + 1
    genres = [{"genre": k, "count": v} for k, v in sorted(genre_counts.items())]

    if genre:
        wanted = genre.strip().casefold()
        cards = [it for it in cards if any((g or "").casefold() == wanted for g in (it.get("genres") or []))]

    ordered = _order_local_anime(cards, sort)

    if not genre and sort in ("trending", "popular"):
        try:
            trending = await fetch_trending_anime(client, limit=20)
        except Exception as e:  # TMDB also down, so skip the top-up
            logger.warning(f"Anime fallback TMDB top-up failed: {e}")
            trending = []
        if trending:
            by_id = {it["anilist_id"]: it for it in ordered if it.get("anilist_id")}
            seen: set = set()
            head = []
            for t in trending:
                aid = t.get("anilist_id")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                local = by_id.get(aid)
                if local:
                    merged = dict(local)
                    if not merged.get("poster") and t.get("poster"):
                        merged["poster"] = t["poster"]
                    head.append(merged)
                else:
                    head.append({
                        "anilist_id": aid, "kind": "anime",
                        "title": t.get("title"), "poster": t.get("poster"),
                        "year": t.get("year"), "genres": [],
                    })
            ordered = head + [it for it in ordered if it.get("anilist_id") not in seen]

    total = len(ordered)
    start = (page - 1) * per_page
    window = ordered[start:start + per_page]
    return {
        "items": window,
        "total": total,
        "page": page,
        "has_next": start + per_page < total,
        "genres": genres,
    }


@router.get("/catalogue/anime")
async def get_anime_catalogue(
    genre: Optional[str] = Query(None, description="Optional AniList genre filter, e.g. Action, Romance"),
    sort: str = Query(CATALOGUE_DEFAULT_SORT, description="trending | popular | score | newest | title"),
    page: int = Query(1, ge=1, le=200, description="1-based page for the browse hub"),
):
    """The fast, default anime browse: one paginated page of AniList anime.

    Distinct from /catalogue, the full local archive the hub keeps as a secondary
    view, because shipping and rendering that whole list is slow. Items are
    ``kind: 'anime'`` cards keyed by anilist_id.
    """
    async with http_client() as client:
        genres = await fetch_anilist_genres(client)
        result = await fetch_anime_catalogue(client, genre=genre, sort=sort, page=page)
        # An AniList failure must not 503. The fetcher first serves the last known
        # good copy of this page as ``stale``; only with no shadow does it report
        # ``unavailable``, and then the local DB takes over so the hub keeps
        # working. ``fallback: true`` lets the client show a gentle notice.
        if result.get("unavailable"):
            fb = await _build_local_anime_fallback(client, genre=genre, sort=sort, page=page)
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
        # AniList was down, but a cached page was served instead.
        "stale": bool(result.get("stale")),
        "genres": [{"genre": g} for g in genres],
        "animes": result["items"],
    }


# --- Non-anime catalogues (shows / movies), from the local TMDB tables -------
# The show and movie twins of /catalogue, built entirely from the local tables
# with the same cache and gzip treatment. Genres always describe the whole
# catalogue so every chip renders, while the list itself honours ?genre=. Kept
# generic because the two differ only in table and list key.

async def _serve_local_catalogue(request, *, cache_prefix, builder, list_key, genre):
    """Shared /catalogue-style responder for a locally-built poster-card list.

    ``builder`` is the sync DB reader, run in a threadpool, and ``list_key`` names
    the item array in the body. The unfiltered body is built once per cache window
    and its bytes reused, while a ``genre`` filter is computed on demand.
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

        # Over the full catalogue and memoized like the anime one. Shows and
        # movies have no format "category" axis.
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
    """The non-anime TV catalogue for the Shows hub, from the local DB only.

    ``kind: 'show'`` cards keyed by tmdb_id, popular first. ``genres`` always
    describes the whole catalogue while ``shows`` honours ``genre``.
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
    """The general-movie catalogue for the Movies hub, from the local DB only.

    ``kind: 'movie'`` cards keyed by tmdb_id and carrying ``vote_average``,
    popular first. Genre facet and filter behave as in /catalogue/shows.
    """
    return await _serve_local_catalogue(
        request, cache_prefix="catalogue:movies", builder=get_movies_catalogue_items,
        list_key="movies", genre=genre,
    )
