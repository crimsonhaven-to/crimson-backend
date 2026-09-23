"""Title pages: show and season detail, the per-title overviews, the AniList
mapping lookups, and the /scrape-meta bundles for the client's discovery
sources. The assembly lives in ``overview.py``."""

from fastapi import APIRouter, Query, Request

from core.rate_limit import limiter

from . import overview

router = APIRouter(tags=["metadata"])


@router.get("/show/{tmdb_id}")
async def get_show_details(tmdb_id: int):
    return await overview.show_details(tmdb_id)


@router.get("/season/{tmdb_id}/{season_number}")
async def get_season_details(tmdb_id: int, season_number: int):
    return await overview.season_details(tmdb_id, season_number)


@router.get("/scrape-meta/movie/{tmdb_id}")
@limiter.limit("60/minute")
async def get_scrape_meta_movie(request: Request, tmdb_id: int):
    """Declared before the TV route, whose {tmdb_id} would otherwise swallow
    "movie" and fail validation."""
    return await overview.scrape_meta_movie(tmdb_id)


@router.get("/scrape-meta/{tmdb_id}/{season_number}")
@limiter.limit("60/minute")
async def get_scrape_meta(request: Request, tmdb_id: int, season_number: int):
    """Login-gated like /watch, so it is no free anonymous metadata service."""
    return await overview.scrape_meta(tmdb_id, season_number)


@router.get("/anilist/{anilist_id}")
async def get_anilist_mapping(anilist_id: int):
    """The { tmdb_id, season_number } an anilist_id maps to."""
    return await overview.anilist_mapping(anilist_id)


@router.get("/info/{tmdb_id}")
async def get_anime_info(tmdb_id: int, season: int = Query(1, ge=1, description="TMDB season number")):
    return await overview.anime_info(tmdb_id, season)


@router.get("/seasons/{anilist_id}")
async def get_anime_seasons(anilist_id: int):
    return await overview.anime_seasons(anilist_id)


@router.get("/overview/{anilist_id}")
async def get_anime_overview(anilist_id: int):
    return await overview.anime_overview(anilist_id)


@router.get("/show-overview/{tmdb_id}")
async def get_show_overview(tmdb_id: int):
    return await overview.show_overview(tmdb_id)


@router.get("/movie-overview/{tmdb_id}")
async def get_movie_overview(tmdb_id: int):
    return await overview.movie_overview(tmdb_id)
