"""Metadata and overview endpoints, plus the client-side scrape grants.

Show and season detail (/show, /season, /info), the aggregated per-title
overviews (/overview by anilist_id, /show-overview and /movie-overview by
tmdb_id), the AniList mapping lookups (/anilist, /seasons), and the /scrape-meta
title bundles the client-side discovery sources need (New System 1.5).
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request

from core.http_client import http_client
from core.rate_limit import limiter
from metadata_engine.tmdb import (
    _tmdb_img,
    fetch_tmdb_show,
    fetch_tmdb_movie,
    fetch_tmdb_metadata,
    fetch_tmdb_localized_titles,
    fetch_tmdb_imdb_id,
)
from metadata_engine.anilist import fetch_anilist_metadata, _empty

from web.queries import (
    get_anilist_id,
    get_tmdb_season,
    get_anime_genres,
    get_show_seasons,
    get_show_extras,
    get_show_info,
    get_movie_info,
)
from web.util import _year_from_date

logger = logging.getLogger("crimson.metadata")

router = APIRouter()


# Shown when a live TMDB fetch failed and the page was rebuilt from local metadata
# only. In Lumi's voice, for the frontend banner.
DEGRADED_OVERVIEW_NOTICE = {
    "kind": "degraded",
    "title": "The Archives Flicker",
    "message": (
        "The Crimson Archives refused to answer for this title, so Lumi has rewoven "
        "this page from her own faded memory. Some seasons, episodes, or art may be "
        "missing until the archive stirs awake. Try again in a little while, mortal."
    ),
}


def _degraded_season_list(tmdb_id: int) -> List[Dict]:
    """Season list built from the stored mapping alone, for when the live TMDB
    fetch is unavailable.

    Enough for the frontend to render season tabs and route the play buttons.
    The TMDB-only fields come back null.
    """
    seasons = []
    for s in get_show_seasons(tmdb_id):
        num = s["season_number"]
        seasons.append({
            "season_number": num,
            "anilist_id": s.get("anilist_id"),
            "tmdb_id": tmdb_id,
            "tmdb_season": num,
            "name": f"Season {num}",
            "poster": None,
            "summary": None,
            "air_date": None,
            "episode_count": None,
            "title_romaji": s.get("title_romaji"),
            "title_english": s.get("title_english"),
            "anime_type": s.get("anime_type"),
        })
    return seasons


def _build_season_list(tmdb_id: int, show: Dict) -> List[Dict]:
    """The per-season list from TMDB's real seasons, with the AniList mapping
    attached. One JOIN supplies every season's mapping and titles, rather than two
    queries per season.
    """
    db_seasons = {s["season_number"]: s for s in get_show_seasons(tmdb_id)}
    seasons = []
    for s in show.get("seasons", []):
        num = s["season_number"]
        mapped = db_seasons.get(num, {})
        seasons.append({
            "season_number": num,
            "anilist_id": mapped.get("anilist_id"),
            "tmdb_id": tmdb_id,
            "tmdb_season": num,
            "name": s["name"],
            "poster": s["poster"] or show.get("poster"),
            "summary": s.get("overview") or show.get("overview"),
            "air_date": s["air_date"],
            "episode_count": s["episode_count"],
            "title_romaji": mapped.get("title_romaji"),
            "title_english": mapped.get("title_english"),
            "anime_type": mapped.get("anime_type"),
        })
    return seasons


@router.get("/show/{tmdb_id}")
async def get_show_details(tmdb_id: int):
    """Show info and every TMDB season, AniList-mapped where known."""
    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    show_info = get_show_info(tmdb_id) or {
        "tmdb_id": tmdb_id,
        "title": show.get("title"),
        "overview": show.get("overview"),
        "poster_path": show.get("poster_path"),
        "backdrop_path": show.get("backdrop_path"),
        "first_air_date": show.get("first_air_date"),
    }

    return {
        "success": True,
        "show": show_info,
        "seasons": _build_season_list(tmdb_id, show),
        "extras": get_show_extras(tmdb_id)
    }


@router.get("/season/{tmdb_id}/{season_number}")
async def get_season_details(tmdb_id: int, season_number: int):
    """TMDB season metadata combined with AniList's, where it exists."""
    anilist_id = get_anilist_id(tmdb_id, season_number)

    async with http_client() as client:
        tmdb_meta, anilist_meta = await asyncio.gather(
            fetch_tmdb_metadata(client, tmdb_id, season_number),
            fetch_anilist_metadata(client, anilist_id) if anilist_id else _empty(),
        )

    if not tmdb_meta and not anilist_meta:
        raise HTTPException(status_code=404, detail=f"No data for TMDB ID {tmdb_id} season {season_number}")

    return {
        "success": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "anilist_id": anilist_id,
        "tmdb_metadata": tmdb_meta,
        "anilist_metadata": anilist_meta
    }


@router.get("/scrape-meta/{tmdb_id}/{season_number}")
@limiter.limit("60/minute")
async def get_scrape_meta(request: Request, tmdb_id: int, season_number: int):
    """The title bundle the client-side discovery sources need.

    The engine in the viewer's browser resolves TMDB-keyed sources off the id
    alone, but the title-matching sources search by title, and the German
    broadcast synonyms come from TMDB /translations, which needs the server-held
    TMDB key. The client merges this grant into its MediaCtx, keeping title
    matching identical to the backend scrapers without the key ever shipping to
    the browser.

    Returns exactly the fields the backend's own ``media_ctx`` carries: primary
    title, the AniList title variants, and the synonyms. Login-gated like /watch,
    so it cannot be used anonymously as a free metadata service."""
    anilist_id = get_anilist_id(tmdb_id, season_number)

    # For the year-disambiguated and IMDb-keyed client sources. Both cached and
    # best-effort.
    release_year, imdb_id = await _show_year_imdb(tmdb_id)

    if anilist_id:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id) or {}
        title = anilist_data.get("title")
        synonyms = list(anilist_data.get("synonyms") or [])
        return {
            "success": True,
            "anilist_id": anilist_id,
            # For the MAL-keyed client source. Already in anilist_data, so this
            # costs no extra fetch.
            "mal_id": anilist_data.get("mal_id"),
            "title": title,
            "title_english": anilist_data.get("title_english"),
            "title_romaji": anilist_data.get("title_romaji"),
            "title_native": anilist_data.get("title_native"),
            "synonyms": synonyms,
            "release_year": release_year,
            "imdb_id": imdb_id,
        }

    # For TMDB-only seasons of long shows the primary title is the TMDB one,
    # enriched with the German broadcast titles those sites list non-anime shows
    # under. The same enrichment the watch stream does in this case.
    info = get_show_info(tmdb_id)
    title = info.get("title") if info else None
    synonyms: List[str] = []
    try:
        async with http_client() as client:
            if not title:
                show = await fetch_tmdb_show(client, tmdb_id)
                title = show.get("title")
            german_titles = await fetch_tmdb_localized_titles(client, tmdb_id)
        synonyms = [t for t in (german_titles or []) if t]
    except Exception as e:
        logger.warning(f"scrape-meta enrichment failed for {tmdb_id}: {e}")

    return {
        "success": True,
        "anilist_id": None,
        "mal_id": None,  # no AniList entry means no MAL id
        "title": title,
        "title_english": title,
        "title_romaji": None,
        "title_native": None,
        "synonyms": synonyms,
        "release_year": release_year,
        "imdb_id": imdb_id,
    }


async def _show_year_imdb(tmdb_id: int) -> Tuple[Optional[int], Optional[str]]:
    """(release_year, imdb_id) for a TMDB show. Best-effort, both cached."""
    try:
        async with http_client() as client:
            show = await fetch_tmdb_show(client, tmdb_id)
            imdb = await fetch_tmdb_imdb_id(client, tmdb_id, "tv")
        return _year_from_date((show or {}).get("first_air_date")), imdb
    except Exception as e:
        logger.warning(f"scrape-meta year/imdb failed for show {tmdb_id}: {e}")
        return None, None


@router.get("/scrape-meta/movie/{tmdb_id}")
@limiter.limit("60/minute")
async def get_scrape_meta_movie(request: Request, tmdb_id: int):
    """The movie twin of /scrape-meta: the title, year and IMDb id the client
    sources need to match a movie. The TMDB key stays server-side, and it is
    login-gated like /watch."""
    title = None
    release_year = None
    imdb_id = None
    try:
        info = get_movie_info(tmdb_id)
        title = info.get("title") if info else None
        async with http_client() as client:
            movie = await fetch_tmdb_movie(client, tmdb_id)
            if not title:
                title = (movie or {}).get("title")
            release_year = _year_from_date((movie or {}).get("release_date"))
            imdb_id = await fetch_tmdb_imdb_id(client, tmdb_id, "movie")
    except Exception as e:
        logger.warning(f"scrape-meta(movie) enrichment failed for {tmdb_id}: {e}")

    return {
        "success": True,
        "anilist_id": None,
        "mal_id": None,  # movies carry no MAL id
        "title": title,
        "title_english": title,
        "title_romaji": None,
        "title_native": None,
        "synonyms": [],
        "release_year": release_year,
        "imdb_id": imdb_id,
    }


@router.get("/anilist/{anilist_id}")
async def get_anilist_mapping(anilist_id: int):
    """The { tmdb_id, season_number } an anilist_id maps to."""
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    return {
        "success": True,
        "anilist_id": anilist_id,
        "tmdb_id": mapping[0],
        "season_number": mapping[1]
    }


# --- COMPATIBILITY ENDPOINTS (legacy frontend contract) ---
@router.get("/info/{tmdb_id}")
async def get_anime_info(tmdb_id: int, season: int = Query(1, ge=1, description="TMDB season number")):
    """Merged TMDB and AniList metadata for a (tmdb_id, season), in the flat legacy
    shape.

    AniList is optional: a season with no entry still returns TMDB metadata and a
    TMDB-derived episode list, and the description falls back through AniList, the
    TMDB season, then the show overview.
    """
    anilist_id = get_anilist_id(tmdb_id, season)

    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)
        # Independent of each other, so fetched concurrently.
        tmdb_data, anilist_data = await asyncio.gather(
            fetch_tmdb_metadata(client, tmdb_id, season, show=show),
            fetch_anilist_metadata(client, anilist_id) if anilist_id else _empty(),
        )

    if not show and not tmdb_data and not anilist_data:
        raise HTTPException(status_code=404, detail=f"No data for TMDB ID {tmdb_id} season {season}")

    available_seasons = [s["season_number"] for s in show.get("seasons", [])]
    if not available_seasons:
        available_seasons = [s["season_number"] for s in get_show_seasons(tmdb_id)]

    # Never return an empty description or episode list.
    description = anilist_data.get("description") or tmdb_data.get("summary") or show.get("overview")

    # TMDB's list is correctly split by season, carries real per-episode detail,
    # and matches the numbering the proxy sources play by. AniList's
    # streamingEpisodes are crowd-sourced and unreliable for sequel seasons, often
    # echoing the first season's titles, which made every season look identical.
    # So AniList is only a fallback when TMDB has no per-episode data.
    tmdb_eps = tmdb_data.get("episodes") or []
    anilist_eps = anilist_data.get("episodes_list") or []
    episodes_list = tmdb_eps or anilist_eps

    return {
        **tmdb_data,
        **anilist_data,
        "success": True,
        "tmdb_id": tmdb_id,
        "anilist_id": anilist_id,
        "current_season": season,
        "available_seasons": available_seasons,
        "description": description,
        "summary": tmdb_data.get("summary") or show.get("overview"),
        "episodes_list": episodes_list,
        "title": anilist_data.get("title") or show.get("title"),
    }


@router.get("/seasons/{anilist_id}")
async def get_anime_seasons(anilist_id: int):
    """Every season of the show this anilist_id belongs to, in the legacy shape.

    Each carries its own tmdb_id and tmdb_season, so the frontend can drill into
    /info and /watch.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id = mapping[0]

    async with http_client() as client:
        show, anime_info = await asyncio.gather(
            fetch_tmdb_show(client, tmdb_id),
            fetch_anilist_metadata(client, anilist_id),
        )
    if not show:
        raise HTTPException(status_code=404, detail="Show not found on TMDB")

    seasons_data = _build_season_list(tmdb_id, show)

    title = (anime_info or {}).get("title") or show.get("title") or "Unknown Anime"

    return {
        "success": True,
        "anilist_id": anilist_id,
        "title": title,
        "total_seasons": len(seasons_data),
        "seasons": seasons_data,
        "extras": get_show_extras(tmdb_id),
    }


@router.get("/overview/{anilist_id}")
async def get_anime_overview(anilist_id: int):
    """Aggregated overview for the per-anime landing page.

    Show-level metadata plus the full season list and extras in one round-trip, so
    the frontend paints the season browser without a /seasons to /info waterfall.

    Per-season episode lists stay lazy via /info, so this never fans out into one
    TMDB call per season.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id = mapping[0]

    async with http_client() as client:
        show, anime_info = await asyncio.gather(
            fetch_tmdb_show(client, tmdb_id),
            fetch_anilist_metadata(client, anilist_id),
        )

    anime_info = anime_info or {}

    # A failed live fetch, such as TMDB 502ing on one broken record, must not
    # 404 the whole page. Given any metadata at all, rebuild a degraded overview
    # and flag it so the frontend can say so.
    degraded = not show
    if degraded:
        stored = get_show_info(tmdb_id)
        if not stored and not anime_info:
            raise HTTPException(status_code=404, detail="Show not found on TMDB")
        show = {
            "title": stored.get("title"),
            "overview": stored.get("overview"),
            "poster": _tmdb_img(stored.get("poster_path")),
            "backdrop": _tmdb_img(stored.get("backdrop_path"), "original"),
            "first_air_date": stored.get("first_air_date"),
            "seasons": [],
        }
        seasons_data = _degraded_season_list(tmdb_id)
    else:
        seasons_data = _build_season_list(tmdb_id, show)

    title = anime_info.get("title") or show.get("title") or "Unknown Anime"

    # TMDB's first-air-date, falling back to AniList's start year.
    year = None
    first_air = show.get("first_air_date")
    if first_air:
        year = first_air[:4]
    elif (anime_info.get("start_date") or {}).get("year"):
        year = str(anime_info["start_date"]["year"])

    return {
        "success": True,
        "anilist_id": anilist_id,
        "tmdb_id": tmdb_id,
        "title": title,
        "title_romaji": anime_info.get("title_romaji"),
        "title_english": anime_info.get("title_english"),
        # AniList cover art is higher quality than the TMDB poster.
        "poster": anime_info.get("cover") or show.get("poster"),
        "backdrop": show.get("backdrop"),
        "banner": anime_info.get("banner"),
        # `description` may contain AniList HTML; `summary` is plain TMDB text.
        "description": anime_info.get("description") or show.get("overview"),
        "summary": show.get("overview"),
        "status": anime_info.get("status"),
        "year": year,
        "total_episodes": anime_info.get("total_episodes"),
        "total_seasons": len(seasons_data),
        # From the local anime DB, the same source as the catalogue. The
        # show-overview twin omits this, so genre tags stay anime-specific.
        "genres": get_anime_genres(anilist_id),
        "seasons": seasons_data,
        "extras": get_show_extras(tmdb_id),
        # Rebuilt from local data only; the frontend renders the notice as a
        # themed banner.
        "degraded": degraded,
        "notice": DEGRADED_OVERVIEW_NOTICE if degraded else None,
    }


@router.get("/show-overview/{tmdb_id}")
async def get_show_overview(tmdb_id: int):
    """Aggregated overview for a non-anime TV show, keyed by tmdb_id.

    The TMDB-keyed twin of /overview/{anilist_id}, sharing its response shape so
    the frontend reuses one overview UI, but built purely from TMDB since a
    general show has no AniList entry. Seasons come from TMDB's real list with the
    anilist_id fields null, and episodes stay lazy via /info.
    """
    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)

    # The twin of /overview's fallback. A general show has no AniList entry, so
    # the stored tmdb_shows row is the only source to rebuild from.
    degraded = not show
    if degraded:
        stored = get_show_info(tmdb_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Show not found on TMDB")
        stored_genres = []
        if stored.get("genres"):
            try:
                stored_genres = json.loads(stored["genres"]) or []
            except (TypeError, ValueError):
                stored_genres = []
        show = {
            "title": stored.get("title"),
            "overview": stored.get("overview"),
            "poster": _tmdb_img(stored.get("poster_path")),
            "backdrop": _tmdb_img(stored.get("backdrop_path"), "original"),
            "first_air_date": stored.get("first_air_date"),
            "genres": stored_genres,
            "seasons": [],
        }
        seasons_data = _degraded_season_list(tmdb_id)
    else:
        seasons_data = _build_season_list(tmdb_id, show)
    year = (show.get("first_air_date") or "")[:4] or None

    return {
        "success": True,
        "kind": "show",
        "anilist_id": None,
        "tmdb_id": tmdb_id,
        "title": show.get("title"),
        "title_romaji": None,
        "title_english": show.get("title"),
        "poster": show.get("poster"),
        "backdrop": show.get("backdrop"),
        "banner": None,
        "description": show.get("overview"),
        "summary": show.get("overview"),
        "status": None,
        "year": year,
        "total_episodes": None,
        "total_seasons": len(seasons_data),
        "seasons": seasons_data,
        # The non-anime twin of /overview's genres.
        "genres": show.get("genres") or [],
        # General shows carry no AniList extras mapping.
        "extras": [],
        # Rebuilt from local data only.
        "degraded": degraded,
        "notice": DEGRADED_OVERVIEW_NOTICE if degraded else None,
    }


@router.get("/movie-overview/{tmdb_id}")
async def get_movie_overview(tmdb_id: int):
    """Aggregated overview for a standalone movie, keyed by its TMDB movie id.

    The movie twin of /show-overview, sharing its response shape but carrying a
    single ``play`` descriptor instead of seasons. Built purely from TMDB, and
    falling back to the stored tmdb_movies row exactly as /show-overview does.
    """
    async with http_client() as client:
        movie = await fetch_tmdb_movie(client, tmdb_id)

    degraded = not movie
    if degraded:
        stored = get_movie_info(tmdb_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Movie not found on TMDB")
        stored_genres = []
        if stored.get("genres"):
            try:
                stored_genres = json.loads(stored["genres"]) or []
            except (TypeError, ValueError):
                stored_genres = []
        movie = {
            "title": stored.get("title"),
            "overview": stored.get("overview"),
            "poster": _tmdb_img(stored.get("poster_path")),
            "backdrop": _tmdb_img(stored.get("backdrop_path"), "original"),
            "release_date": stored.get("release_date"),
            "runtime": None,
            "genres": stored_genres,
            "vote_average": None,
            "status": None,
        }
    year = (movie.get("release_date") or "")[:4] or None

    return {
        "success": True,
        "kind": "movie",
        "anilist_id": None,
        "tmdb_id": tmdb_id,
        "title": movie.get("title"),
        "title_romaji": None,
        "title_english": movie.get("title"),
        "poster": movie.get("poster"),
        "backdrop": movie.get("backdrop"),
        "banner": None,
        "description": movie.get("overview"),
        "summary": movie.get("overview"),
        "status": movie.get("status"),
        "year": year,
        # Movie-specific fields the overview UI may show.
        "runtime": movie.get("runtime"),
        "genres": movie.get("genres") or [],
        "vote_average": movie.get("vote_average"),
        # A movie has no seasons; the page plays the single feature.
        "total_episodes": None,
        "total_seasons": 0,
        "seasons": [],
        "extras": [],
        # The frontend links this to /watch-movie/{id}.
        "play": {"tmdb_id": tmdb_id, "media_type": "movie"},
        "degraded": degraded,
        "notice": DEGRADED_OVERVIEW_NOTICE if degraded else None,
    }
