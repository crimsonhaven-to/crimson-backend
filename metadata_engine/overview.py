"""The title pages: show and season detail, the per-title overviews, and the
title bundles the client's discovery sources need.

TMDB is the live source and the local tables are the fallback. An overview whose
TMDB fetch failed is rebuilt from the stored rows and flagged ``degraded``
rather than 404ing the page. Database reads run off the event loop.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from core.http_client import http_client

from . import catalogue
from .anilist import empty, fetch_anilist_metadata
from .dates import year_from_date
from .tmdb import (
    fetch_tmdb_imdb_id,
    fetch_tmdb_localized_titles,
    fetch_tmdb_metadata,
    fetch_tmdb_movie,
    fetch_tmdb_show,
    tmdb_img,
)

logger = logging.getLogger("crimson.overview")

# In Lumi's voice, for the client's banner on a page rebuilt from local data.
DEGRADED_OVERVIEW_NOTICE = {
    "kind": "degraded",
    "title": "The Archives Flicker",
    "message": (
        "The Crimson Archives refused to answer for this title, so Lumi has rewoven "
        "this page from her own faded memory. Some seasons, episodes, or art may be "
        "missing until the archive stirs awake. Try again in a little while, mortal."
    ),
}


def _not_mapped() -> HTTPException:
    return HTTPException(status_code=404, detail="AniList ID not mapped")


# --- seasons ----------------------------------------------------------------------
def _season_entry(tmdb_id: int, num: int, mapped: Dict, **tmdb_fields) -> Dict:
    return {
        "season_number": num,
        "anilist_id": mapped.get("anilist_id"),
        "tmdb_id": tmdb_id,
        "tmdb_season": num,
        **tmdb_fields,
        "title_romaji": mapped.get("title_romaji"),
        "title_english": mapped.get("title_english"),
        "anime_type": mapped.get("anime_type"),
    }


async def season_list(tmdb_id: int, show: Optional[Dict]) -> List[Dict]:
    """TMDB's real seasons with the AniList mapping attached, or, without a live
    show, just the mapped seasons with the TMDB-only fields left null."""
    stored = await asyncio.to_thread(catalogue.get_show_seasons, tmdb_id)
    if not show:
        return [
            _season_entry(
                tmdb_id, s["season_number"], s, name=f"Season {s['season_number']}",
                poster=None, summary=None, air_date=None, episode_count=None,
            )
            for s in stored
        ]
    by_number = {s["season_number"]: s for s in stored}
    return [
        _season_entry(
            tmdb_id, s["season_number"], by_number.get(s["season_number"], {}),
            name=s["name"],
            poster=s["poster"] or show.get("poster"),
            summary=s.get("overview") or show.get("overview"),
            air_date=s["air_date"],
            episode_count=s["episode_count"],
        )
        for s in show.get("seasons", [])
    ]


def _from_stored(stored: Dict, date_field: str, **extra) -> Dict:
    return {
        "title": stored.get("title"),
        "overview": stored.get("overview"),
        "poster": tmdb_img(stored.get("poster_path")),
        "backdrop": tmdb_img(stored.get("backdrop_path"), "original"),
        date_field: stored.get(date_field),
        **extra,
    }


# --- show and season detail ----------------------------------------------------------
async def show_details(tmdb_id: int) -> Dict:
    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    stored = await asyncio.to_thread(catalogue.get_show_info, tmdb_id)
    return {
        "success": True,
        "show": stored or {
            "tmdb_id": tmdb_id,
            "title": show.get("title"),
            "overview": show.get("overview"),
            "poster_path": show.get("poster_path"),
            "backdrop_path": show.get("backdrop_path"),
            "first_air_date": show.get("first_air_date"),
        },
        "seasons": await season_list(tmdb_id, show),
        "extras": await asyncio.to_thread(catalogue.get_show_extras, tmdb_id),
    }


async def season_details(tmdb_id: int, season_number: int) -> Dict:
    anilist_id = await asyncio.to_thread(catalogue.get_anilist_id, tmdb_id, season_number)
    async with http_client() as client:
        tmdb_meta, anilist_meta = await asyncio.gather(
            fetch_tmdb_metadata(client, tmdb_id, season_number),
            fetch_anilist_metadata(client, anilist_id) if anilist_id else empty(),
        )
    if not tmdb_meta and not anilist_meta:
        raise HTTPException(status_code=404, detail=f"No data for TMDB ID {tmdb_id} season {season_number}")
    return {
        "success": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "anilist_id": anilist_id,
        "tmdb_metadata": tmdb_meta,
        "anilist_metadata": anilist_meta,
    }


async def anilist_mapping(anilist_id: int) -> Dict:
    mapping = await asyncio.to_thread(catalogue.get_tmdb_season, anilist_id)
    if not mapping:
        raise _not_mapped()
    return {"success": True, "anilist_id": anilist_id, "tmdb_id": mapping[0], "season_number": mapping[1]}


async def anime_info(tmdb_id: int, season: int) -> Dict:
    """TMDB and AniList merged into the flat legacy /info shape. AniList is
    optional, and the description falls back through AniList, the TMDB season,
    then the show overview."""
    anilist_id = await asyncio.to_thread(catalogue.get_anilist_id, tmdb_id, season)
    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)
        tmdb_data, anilist_data = await asyncio.gather(
            fetch_tmdb_metadata(client, tmdb_id, season, show=show),
            fetch_anilist_metadata(client, anilist_id) if anilist_id else empty(),
        )
    if not show and not tmdb_data and not anilist_data:
        raise HTTPException(status_code=404, detail=f"No data for TMDB ID {tmdb_id} season {season}")

    available = [s["season_number"] for s in show.get("seasons", [])]
    if not available:
        stored = await asyncio.to_thread(catalogue.get_show_seasons, tmdb_id)
        available = [s["season_number"] for s in stored]

    # TMDB's episodes are split by season correctly and numbered the way the
    # sources play. AniList's streamingEpisodes are crowd-sourced and often echo
    # the first season's titles for sequels, so they are only a fallback.
    episodes = tmdb_data.get("episodes") or anilist_data.get("episodes_list") or []
    return {
        **tmdb_data,
        **anilist_data,
        "success": True,
        "tmdb_id": tmdb_id,
        "anilist_id": anilist_id,
        "current_season": season,
        "available_seasons": available,
        "description": anilist_data.get("description") or tmdb_data.get("summary") or show.get("overview"),
        "summary": tmdb_data.get("summary") or show.get("overview"),
        "episodes_list": episodes,
        "title": anilist_data.get("title") or show.get("title"),
    }


async def anime_seasons(anilist_id: int) -> Dict:
    """Every season of the show an anilist_id belongs to, in the legacy shape."""
    mapping = await asyncio.to_thread(catalogue.get_tmdb_season, anilist_id)
    if not mapping:
        raise _not_mapped()
    tmdb_id = mapping[0]
    async with http_client() as client:
        show, anime = await asyncio.gather(
            fetch_tmdb_show(client, tmdb_id), fetch_anilist_metadata(client, anilist_id)
        )
    if not show:
        raise HTTPException(status_code=404, detail="Show not found on TMDB")
    seasons = await season_list(tmdb_id, show)
    return {
        "success": True,
        "anilist_id": anilist_id,
        "title": (anime or {}).get("title") or show.get("title") or "Unknown Anime",
        "total_seasons": len(seasons),
        "seasons": seasons,
        "extras": await asyncio.to_thread(catalogue.get_show_extras, tmdb_id),
    }


# --- overviews -----------------------------------------------------------------------
def _title_page(**fields) -> Dict:
    """The response shape every overview shares, so the client uses one page."""
    return {
        "success": True,
        "anilist_id": None,
        "title_romaji": None,
        "banner": None,
        "status": None,
        "total_episodes": None,
        "extras": [],
        **fields,
        "notice": DEGRADED_OVERVIEW_NOTICE if fields.get("degraded") else None,
    }


async def anime_overview(anilist_id: int) -> Dict:
    """Show metadata, every season and the extras in one round trip, so the page
    paints without a /seasons then /info waterfall. Episode lists stay lazy."""
    mapping = await asyncio.to_thread(catalogue.get_tmdb_season, anilist_id)
    if not mapping:
        raise _not_mapped()
    tmdb_id = mapping[0]
    async with http_client() as client:
        show, anime = await asyncio.gather(
            fetch_tmdb_show(client, tmdb_id), fetch_anilist_metadata(client, anilist_id)
        )
    anime = anime or {}

    degraded = not show
    if degraded:
        stored = await asyncio.to_thread(catalogue.get_show_info, tmdb_id)
        if not stored and not anime:
            raise HTTPException(status_code=404, detail="Show not found on TMDB")
        show = _from_stored(stored, "first_air_date", seasons=[])
    seasons = await season_list(tmdb_id, None if degraded else show)

    first_air = show.get("first_air_date")
    start_year = (anime.get("start_date") or {}).get("year")
    return _title_page(
        anilist_id=anilist_id,
        tmdb_id=tmdb_id,
        title=anime.get("title") or show.get("title") or "Unknown Anime",
        title_romaji=anime.get("title_romaji"),
        title_english=anime.get("title_english"),
        # AniList's cover art is sharper than TMDB's poster.
        poster=anime.get("cover") or show.get("poster"),
        backdrop=show.get("backdrop"),
        banner=anime.get("banner"),
        # `description` may carry AniList HTML; `summary` is plain TMDB text.
        description=anime.get("description") or show.get("overview"),
        summary=show.get("overview"),
        status=anime.get("status"),
        year=first_air[:4] if first_air else (str(start_year) if start_year else None),
        total_episodes=anime.get("total_episodes"),
        total_seasons=len(seasons),
        genres=await asyncio.to_thread(catalogue.get_anime_genres, anilist_id),
        seasons=seasons,
        extras=await asyncio.to_thread(catalogue.get_show_extras, tmdb_id),
        degraded=degraded,
    )


async def show_overview(tmdb_id: int) -> Dict:
    """The TMDB-keyed twin of the anime overview, for a show with no AniList entry."""
    async with http_client() as client:
        show = await fetch_tmdb_show(client, tmdb_id)
    degraded = not show
    if degraded:
        stored = await asyncio.to_thread(catalogue.get_show_info, tmdb_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Show not found on TMDB")
        show = _from_stored(stored, "first_air_date", genres=catalogue.decode_genres(stored.get("genres")), seasons=[])
    seasons = await season_list(tmdb_id, None if degraded else show)
    return _title_page(
        kind="show",
        tmdb_id=tmdb_id,
        title=show.get("title"),
        title_english=show.get("title"),
        poster=show.get("poster"),
        backdrop=show.get("backdrop"),
        description=show.get("overview"),
        summary=show.get("overview"),
        year=(show.get("first_air_date") or "")[:4] or None,
        total_seasons=len(seasons),
        seasons=seasons,
        genres=show.get("genres") or [],
        degraded=degraded,
    )


async def movie_overview(tmdb_id: int) -> Dict:
    """The movie twin of the show overview, with a ``play`` descriptor instead of
    seasons."""
    async with http_client() as client:
        movie = await fetch_tmdb_movie(client, tmdb_id)
    degraded = not movie
    if degraded:
        stored = await asyncio.to_thread(catalogue.get_movie_info, tmdb_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Movie not found on TMDB")
        movie = _from_stored(
            stored, "release_date", runtime=None, vote_average=None, status=None,
            genres=catalogue.decode_genres(stored.get("genres")),
        )
    return _title_page(
        kind="movie",
        tmdb_id=tmdb_id,
        title=movie.get("title"),
        title_english=movie.get("title"),
        poster=movie.get("poster"),
        backdrop=movie.get("backdrop"),
        description=movie.get("overview"),
        summary=movie.get("overview"),
        status=movie.get("status"),
        year=(movie.get("release_date") or "")[:4] or None,
        runtime=movie.get("runtime"),
        genres=movie.get("genres") or [],
        vote_average=movie.get("vote_average"),
        total_seasons=0,
        seasons=[],
        play={"tmdb_id": tmdb_id, "media_type": "movie"},
        degraded=degraded,
    )


# --- title bundles for the client's discovery sources -----------------------------------
def _bundle(**fields) -> Dict:
    return {
        "success": True,
        "anilist_id": None,
        "mal_id": None,
        "title_romaji": None,
        "title_native": None,
        "synonyms": [],
        **fields,
    }


async def _show_year_imdb(tmdb_id: int) -> Tuple[Optional[int], Optional[str]]:
    try:
        async with http_client() as client:
            show = await fetch_tmdb_show(client, tmdb_id)
            imdb = await fetch_tmdb_imdb_id(client, tmdb_id, "tv")
        return year_from_date((show or {}).get("first_air_date")), imdb
    except Exception as e:
        logger.warning(f"scrape-meta year/imdb failed for show {tmdb_id}: {e}")
        return None, None


async def scrape_meta(tmdb_id: int, season_number: int) -> Dict:
    """The titles, year and IMDb id the browser's discovery sources match on. The
    German broadcast synonyms need the server-held TMDB key, which is why this
    exists: the client merges it into its MediaCtx and the key never ships."""
    anilist_id = await asyncio.to_thread(catalogue.get_anilist_id, tmdb_id, season_number)
    release_year, imdb_id = await _show_year_imdb(tmdb_id)

    if anilist_id:
        async with http_client() as client:
            anime = await fetch_anilist_metadata(client, anilist_id) or {}
        return _bundle(
            anilist_id=anilist_id,
            mal_id=anime.get("mal_id"),
            title=anime.get("title"),
            title_english=anime.get("title_english"),
            title_romaji=anime.get("title_romaji"),
            title_native=anime.get("title_native"),
            synonyms=list(anime.get("synonyms") or []),
            release_year=release_year,
            imdb_id=imdb_id,
        )

    # A TMDB-only season: the TMDB title plus the German broadcast titles those
    # sites list non-anime shows under.
    info = await asyncio.to_thread(catalogue.get_show_info, tmdb_id)
    title = info.get("title")
    synonyms: List[str] = []
    try:
        async with http_client() as client:
            if not title:
                title = (await fetch_tmdb_show(client, tmdb_id)).get("title")
            synonyms = [t for t in (await fetch_tmdb_localized_titles(client, tmdb_id) or []) if t]
    except Exception as e:
        logger.warning(f"scrape-meta enrichment failed for {tmdb_id}: {e}")
    return _bundle(
        title=title, title_english=title, synonyms=synonyms,
        release_year=release_year, imdb_id=imdb_id,
    )


async def scrape_meta_movie(tmdb_id: int) -> Dict:
    title = release_year = imdb_id = None
    try:
        title = (await asyncio.to_thread(catalogue.get_movie_info, tmdb_id)).get("title")
        async with http_client() as client:
            movie = await fetch_tmdb_movie(client, tmdb_id) or {}
            title = title or movie.get("title")
            release_year = year_from_date(movie.get("release_date"))
            imdb_id = await fetch_tmdb_imdb_id(client, tmdb_id, "movie")
    except Exception as e:
        logger.warning(f"scrape-meta(movie) enrichment failed for {tmdb_id}: {e}")
    return _bundle(title=title, title_english=title, release_year=release_year, imdb_id=imdb_id)
