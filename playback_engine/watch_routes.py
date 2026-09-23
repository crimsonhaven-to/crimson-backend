"""The progressive NDJSON /watch streams the player reads, one line per source."""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request

from core.public_url import public_base_url
from core.rate_limit import limiter
from metadata_engine import catalogue

from . import ndjson
from .pipeline import watch_events
from .titles import movie_title, tv_start

router = APIRouter(tags=["watch"])


def _stream(events):
    async def _lines():
        async for event in events:
            yield ndjson.line(event)
    return ndjson.response(_lines())


@router.get("/watch/{tmdb_id}/{season_number}/{episode_number}")
@limiter.limit("30/minute")
async def get_watch_links(request: Request, tmdb_id: int, season_number: int, episode_number: int):
    anilist_id, fallback_title = await tv_start(tmdb_id, season_number)
    return _stream(watch_events(
        tmdb_id, season_number, episode_number, anilist_id, fallback_title,
        base_url=public_base_url(request),
    ))


@router.get("/watch/movie/{tmdb_id}")
@limiter.limit("30/minute")
async def get_movie_watch_links(request: Request, tmdb_id: int):
    """Declared before /watch/{anilist_id}/{episode_number}, which would
    otherwise take the literal "movie" and fail to parse it."""
    return _stream(watch_events(
        tmdb_id, None, None, None, await movie_title(tmdb_id),
        base_url=public_base_url(request), media_type="movie",
    ))


@router.get("/watch/{anilist_id}/{episode_number}")
@limiter.limit("30/minute")
async def deprecated_watch(request: Request, anilist_id: int, episode_number: int, season_part: int = Query(1)):
    """Watch by anilist_id, served directly rather than redirected: WebKit drops
    the Authorization header when fetch() follows a redirect, which would bounce
    the viewer off the login wall."""
    mapping = await asyncio.to_thread(catalogue.get_tmdb_season, anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")
    tmdb_id, season_number = mapping
    base_url = public_base_url(request)

    # An extra that is a film in its own right plays through the movie pipeline,
    # so movie sources can find it and its cache key cannot collide with an
    # episode of the parent show.
    if season_number is None:
        movie_id = await asyncio.to_thread(catalogue.get_extra_movie_id, anilist_id)
        if movie_id:
            title = (await asyncio.to_thread(catalogue.get_movie_info, movie_id)).get("title")
            return _stream(watch_events(movie_id, None, None, None, title, base_url=base_url, media_type="movie"))

    # Any other extra plays as season 0, the specials season TMDB and the sites use.
    return _stream(watch_events(
        tmdb_id, season_number if season_number is not None else 0, episode_number, anilist_id,
        base_url=base_url,
    ))
