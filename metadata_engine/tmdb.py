"""
TMDB metadata fetchers, lifted out of api.py.

All TMDB HTTP access (show/movie/season metadata, search, trending, genre map,
localized titles) plus the small ``_tmdb_img`` URL helper. Pure remote-metadata
concern — it never touches the mapping DB (metadata_engine.db_handler) — so it
imports only the shared config / HTTP client / response cache from core.
"""

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

from core.config import Config
from core.http_client import http_client, fetch_with_retry
from core.response_cache import (
    _local_get,
    _local_set,
    get_cached_response,
    set_cached_response,
)

from metadata_engine.store import (
    get_first_anilist_ids,
    upsert_show_info,
    upsert_movie_info,
    _persist_discovered_show,
    _persist_discovered_movie,
)

logger = logging.getLogger("crimson.tmdb")


# Bump when the cached TMDB payload shape changes, so stale entries in the
# volume-persisted api_cache are ignored after a deploy instead of served.
TMDB_CACHE_VERSION = "v3"


def _tmdb_img(path: Optional[str], size: str = "w500") -> Optional[str]:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


async def fetch_tmdb_genre_map(client: httpx.AsyncClient, kind: str) -> Dict[int, str]:
    """TMDB genre id -> name map for ``kind`` ('tv' or 'movie'), cached.

    Discover/search results carry only ``genre_ids`` (ints), not names. This map
    turns them into the genre-name lists we store in tmdb_shows/tmdb_movies (the
    non-anime twin of anime_entries.genres), so the recommend engine can score
    shows/movies the same way it scores anime. The map is tiny and very stable, so
    it's cached aggressively (L1 + DB)."""
    if kind not in ("tv", "movie"):
        return {}
    cache_key = f"tmdb:genremap:{kind}"
    local = _local_get(cache_key)
    if local is not None:
        return local
    cached = await get_cached_response(cache_key)
    if cached and "map" in cached:
        gmap = {int(k): v for k, v in cached["map"].items()}
        _local_set(cache_key, gmap)
        return gmap

    data = await fetch_with_retry(
        client, f"https://api.themoviedb.org/3/genre/{kind}/list", params={"language": "en-US"}
    )
    gmap = {g["id"]: g["name"] for g in (data or {}).get("genres", []) if g.get("id") and g.get("name")}
    if gmap:
        await set_cached_response(
            cache_key, {"map": {str(k): v for k, v in gmap.items()}},
            ttl_seconds=Config.CACHE_TTL_SECONDS,
        )
        _local_set(cache_key, gmap)
    return gmap


async def fetch_tmdb_show(client: httpx.AsyncClient, tmdb_id: int,
                          force_refresh: bool = False) -> Dict:
    """
    Fetch a TMDB show with its real season list (the authority for what the
    TMDB-keyed sources can play). Cached, and persists core fields into tmdb_shows
    on first fetch.

    ``force_refresh`` skips the cached-response shortcut so the row is re-pulled
    from TMDB and re-upserted (used by the staleness refresher).
    """
    cache_key = f"tmdb:show:{TMDB_CACHE_VERSION}:{tmdb_id}"
    if not force_refresh:
        cached_data = await get_cached_response(cache_key)
        if cached_data:
            return cached_data

    data = await fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}")
    if not data:
        return {}

    seasons = []
    for s in data.get("seasons", []):
        num = s.get("season_number")
        # Skip specials (season 0) and empty placeholder seasons.
        if num is None or num < 1 or (s.get("episode_count") or 0) < 1:
            continue
        seasons.append({
            "season_number": num,
            "name": s.get("name") or f"Season {num}",
            "episode_count": s.get("episode_count"),
            "air_date": s.get("air_date"),
            "poster": _tmdb_img(s.get("poster_path")),
            "overview": s.get("overview"),
        })

    result = {
        "tmdb_id": tmdb_id,
        "title": data.get("name") or data.get("original_name"),
        "overview": data.get("overview"),
        "poster_path": data.get("poster_path"),
        "backdrop_path": data.get("backdrop_path"),
        "poster": _tmdb_img(data.get("poster_path")),
        "backdrop": _tmdb_img(data.get("backdrop_path"), "original"),
        "first_air_date": data.get("first_air_date"),
        # Genre names — the non-anime twin of anime_entries.genres. Stored into
        # tmdb_shows so the recommend engine can score shows by genre too.
        "genres": [g.get("name") for g in (data.get("genres") or []) if g.get("name")],
        "seasons": seasons,
    }

    upsert_show_info({k: result.get(k) for k in
                      ("tmdb_id", "title", "overview", "poster_path", "backdrop_path", "first_air_date", "genres")})
    await set_cached_response(cache_key, result)
    return result


async def fetch_tmdb_movie(client: httpx.AsyncClient, tmdb_id: int,
                           force_refresh: bool = False) -> Dict:
    """Fetch a TMDB *movie* (the /movie/{id} entity — a different id space from
    /tv). Cached, and persists core fields into tmdb_movies on first fetch so the
    overview/watch pages can degrade gracefully when TMDB is unavailable.

    Movies have no seasons/episodes; the TMDB-keyed sources play them off the bare
    movie id, so this is all the metadata the movie surface needs. ``force_refresh``
    skips the cache shortcut so the staleness refresher can re-pull + re-upsert."""
    cache_key = f"tmdb:movie:{TMDB_CACHE_VERSION}:{tmdb_id}"
    if not force_refresh:
        cached_data = await get_cached_response(cache_key)
        if cached_data:
            return cached_data

    data = await fetch_with_retry(client, f"https://api.themoviedb.org/3/movie/{tmdb_id}")
    if not data:
        return {}

    result = {
        "tmdb_id": tmdb_id,
        "title": data.get("title") or data.get("original_title"),
        "overview": data.get("overview"),
        "poster_path": data.get("poster_path"),
        "backdrop_path": data.get("backdrop_path"),
        "poster": _tmdb_img(data.get("poster_path")),
        "backdrop": _tmdb_img(data.get("backdrop_path"), "original"),
        "release_date": data.get("release_date"),
        "original_title": data.get("original_title"),
        "runtime": data.get("runtime"),
        "genres": [g.get("name") for g in (data.get("genres") or []) if g.get("name")],
        "vote_average": data.get("vote_average"),
        "status": data.get("status"),
    }

    upsert_movie_info({k: result.get(k) for k in
                       ("tmdb_id", "title", "overview", "poster_path", "backdrop_path", "release_date",
                        "genres", "runtime", "vote_average", "status", "original_title")})
    await set_cached_response(cache_key, result)
    return result


async def fetch_tmdb_metadata(client: httpx.AsyncClient, tmdb_id: int, season: int = 1,
                              show: Optional[Dict] = None) -> Dict:
    """Fetch metadata + episode list for a specific TMDB season.

    Falls back to show-level overview when the season overview is empty (common
    for anime) so a description is always available. ``show`` may be passed in by
    a caller that already fetched it (e.g. /info), avoiding a redundant cached
    re-fetch; otherwise it is fetched here.
    """
    cache_key = f"tmdb:meta:{TMDB_CACHE_VERSION}:{tmdb_id}:s{season}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    data = await fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}")
    if show is None:
        show = await fetch_tmdb_show(client, tmdb_id)  # cached

    if not data:
        logger.info(f"Season {season} not found for TMDB ID {tmdb_id}, falling back to show metadata")
        result = {
            "summary": show.get("overview"),
            "poster": show.get("poster"),
            "backdrop": show.get("backdrop"),
            "season_name": f"Season {season}",
            "air_date": None,
            "episodes": [],
        }
    else:
        episodes = [{
            "episode_number": ep.get("episode_number"),
            "title": ep.get("name") or f"Episode {ep.get('episode_number')}",
            "thumbnail": _tmdb_img(ep.get("still_path")),
            "overview": ep.get("overview"),
            "air_date": ep.get("air_date"),
            "url": None,
        } for ep in data.get("episodes", [])]

        result = {
            "summary": data.get("overview") or show.get("overview"),
            "poster": _tmdb_img(data.get("poster_path")) or show.get("poster"),
            "backdrop": show.get("backdrop"),
            "season_name": data.get("name") or f"Season {season}",
            "air_date": data.get("air_date"),
            "episodes": episodes,
        }

    if result:
        await set_cached_response(cache_key, result)

    return result


async def _season_episode_info(tmdb_id: int, season_number: int) -> Dict:
    """{count, air_dates:{ep_num: 'YYYY-MM-DD'|None}} for a TMDB season.

    Derived from the (DB-cached) TMDB season metadata and additionally held in the
    in-process L1 cache, since both the unaired gate and the progress enricher hit
    it on hot paths. Best-effort: returns {} when the season can't be loaded so
    callers degrade gracefully (no episode-count gating, no unaired check)."""
    key = f"epinfo:{tmdb_id}:s{season_number}"
    cached = _local_get(key)
    if cached is not None:
        return cached
    try:
        async with http_client() as client:
            meta = await fetch_tmdb_metadata(client, tmdb_id, season_number)
    except Exception as e:
        logger.warning(f"season episode-info fetch failed for {tmdb_id} s{season_number}: {e}")
        return {}
    eps = meta.get("episodes") or []
    info = {
        "count": len(eps),
        "air_dates": {e.get("episode_number"): e.get("air_date") for e in eps},
    }
    _local_set(key, info)
    return info


async def fetch_tmdb_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for anime titles"""
    cache_key = f"tmdb:search:{query.lower()}"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])
    
    url = "https://api.themoviedb.org/3/search/tv"
    data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})
    
    if not data:
        return []

    items = data.get("results", [])[:limit]
    # One batched lookup for every candidate's anilist mapping instead of a query
    # per result.
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])

    results = []
    for item in items:
        tmdb_id = item.get("id")
        anilist_id = anilist_by_tmdb.get(tmdb_id) if tmdb_id else None
        if anilist_id:
            results.append({
                "title": item.get("name") or item.get("original_name"),
                "tmdb_id": tmdb_id,
                "anilist_id": anilist_id,
                "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
                "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
                "vote_average": item.get("vote_average")
            })

    # Cache search results for 24 hours
    await set_cached_response(cache_key, {"results": results}, ttl_seconds=Config.CACHE_TTL_SECONDS)
    return results


async def fetch_trending_anime(client: httpx.AsyncClient, limit: int = 12) -> List[Dict]:
    """Fetch trending anime from TMDB"""
    cache_key = "tmdb:trending"

    # L1: in-process cache (no DB round-trip on a hit).
    local = _local_get(cache_key)
    if local is not None:
        return local

    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        results = cached_data.get("results", [])
        _local_set(cache_key, results)
        return results

    url = "https://api.themoviedb.org/3/discover/tv"
    params = {
        "page": 1,
        "include_adult": "false",
        "language": "en-US",
        "with_genres": "16",  # Animation genre
        "with_original_language": "ja",  # Japanese originals
        "sort_by": "popularity.desc",
        "vote_count.gte": 100  # Minimum votes for quality filter
    }
    
    data = await fetch_with_retry(client, url, params=params)
    
    if not data:
        return []

    items = data.get("results", [])[:limit]
    # One batched lookup for every candidate's anilist mapping instead of a query
    # per result.
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])

    trending_list = []
    for item in items:
        tmdb_id = item.get("id")
        anilist_id = anilist_by_tmdb.get(tmdb_id) if tmdb_id else None
        if anilist_id:
            trending_list.append({
                "title": item.get("name") or item.get("original_name"),
                "tmdb_id": tmdb_id,
                "anilist_id": anilist_id,
                "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
                "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
                "vote_average": item.get("vote_average")
            })

    # Cache trending results (DB for cross-replica reuse + L1 for this process).
    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
    _local_set(cache_key, trending_list)
    return trending_list


async def fetch_tmdb_show_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for general TV shows, excluding anime.

    Excludes (a) titles that already map to an AniList entry — those are anime,
    served by /search/anime — and (b) anything that looks like Japanese animation,
    so unmapped anime doesn't leak into the shows surface."""
    cache_key = f"tmdb:search_shows:{query.lower()}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])

    url = "https://api.themoviedb.org/3/search/tv"
    data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})
    if not data:
        return []

    items = data.get("results", [])
    # One batched lookup so we can drop anything already mapped as anime.
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])
    genre_map = await fetch_tmdb_genre_map(client, "tv")

    results: List[Dict] = []
    for item in items:
        tmdb_id = item.get("id")
        if not tmdb_id or anilist_by_tmdb.get(tmdb_id) or _looks_like_anime(item):
            continue
        if not item.get("poster_path"):
            continue  # posterless rows are usually junk/duplicates — skip for a clean grid
        # Cache the show's metadata + genres so it can seed recommendations later.
        _persist_discovered_show(item, genre_map)
        results.append(_show_item(item))
        if len(results) >= limit:
            break

    await set_cached_response(cache_key, {"results": results}, ttl_seconds=Config.CACHE_TTL_SECONDS)
    return results


async def fetch_trending_shows(client: httpx.AsyncClient, limit: int = 10) -> List[Dict]:
    """Fetch trending non-anime TV shows from TMDB (popular, excluding animation)."""
    cache_key = "tmdb:trending_shows"

    local = _local_get(cache_key)
    if local is not None:
        return local

    cached_data = await get_cached_response(cache_key)
    if cached_data:
        results = cached_data.get("results", [])
        _local_set(cache_key, results)
        return results

    url = "https://api.themoviedb.org/3/discover/tv"
    params = {
        "page": 1,
        "include_adult": "false",
        "language": "en-US",
        "without_genres": "16",          # exclude Animation (keeps anime out)
        "sort_by": "popularity.desc",
        "vote_count.gte": 200,           # quality floor
    }
    data = await fetch_with_retry(client, url, params=params)
    if not data:
        return []

    items = data.get("results", [])
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])
    genre_map = await fetch_tmdb_genre_map(client, "tv")

    trending_list: List[Dict] = []
    for item in items:
        tmdb_id = item.get("id")
        if not tmdb_id or anilist_by_tmdb.get(tmdb_id) or _looks_like_anime(item):
            continue
        if not item.get("poster_path"):
            continue
        # Popular shows make the best recommendation candidates — cache them.
        _persist_discovered_show(item, genre_map)
        trending_list.append(_show_item(item))
        if len(trending_list) >= limit:
            break

    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
    _local_set(cache_key, trending_list)
    return trending_list


async def fetch_tmdb_movie_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for general movies, excluding Japanese animation (anime films
    stay on the anime surface). Posterless rows are dropped for a clean grid."""
    cache_key = f"tmdb:search_movies:{query.lower()}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])

    url = "https://api.themoviedb.org/3/search/movie"
    data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})
    if not data:
        return []

    genre_map = await fetch_tmdb_genre_map(client, "movie")
    results: List[Dict] = []
    for item in data.get("results", []):
        if not item.get("id") or _looks_like_anime_movie(item):
            continue
        if not item.get("poster_path"):
            continue
        _persist_discovered_movie(item, genre_map)
        results.append(_movie_item(item))
        if len(results) >= limit:
            break

    await set_cached_response(cache_key, {"results": results}, ttl_seconds=Config.CACHE_TTL_SECONDS)
    return results


async def fetch_trending_movies(client: httpx.AsyncClient, limit: int = 10) -> List[Dict]:
    """Fetch trending general movies from TMDB (popular, excluding animation)."""
    cache_key = "tmdb:trending_movies"

    local = _local_get(cache_key)
    if local is not None:
        return local

    cached_data = await get_cached_response(cache_key)
    if cached_data:
        results = cached_data.get("results", [])
        _local_set(cache_key, results)
        return results

    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "page": 1,
        "include_adult": "false",
        "language": "en-US",
        "without_genres": "16",          # exclude Animation (keeps anime films out)
        "sort_by": "popularity.desc",
        "vote_count.gte": 300,           # quality floor
    }
    data = await fetch_with_retry(client, url, params=params)
    if not data:
        return []

    genre_map = await fetch_tmdb_genre_map(client, "movie")
    trending_list: List[Dict] = []
    for item in data.get("results", []):
        if not item.get("id") or _looks_like_anime_movie(item):
            continue
        if not item.get("poster_path"):
            continue
        _persist_discovered_movie(item, genre_map)
        trending_list.append(_movie_item(item))
        if len(trending_list) >= limit:
            break

    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
    _local_set(cache_key, trending_list)
    return trending_list


# --- SCRAPER HELPER FUNCTIONS ---
async def fetch_tmdb_localized_titles(client: httpx.AsyncClient, tmdb_id: int) -> List[str]:
    """German-language titles for a TMDB show, for the German scraper sites.

    The German streaming sources (s.to, aniworld) list many shows under their
    *German broadcast title*, not the English one TMDB hands us first — e.g. NCIS
    is "Navy CIS" on s.to, so plain title matching misses the show entirely. We
    pull the German title(s) from TMDB so the title-based scrapers get them as
    extra search candidates: the localized name from ``/translations`` (de) plus
    any DE/AT/CH entries in ``/alternative_titles``. Cached (these are stable);
    returns an empty list on failure (matching just falls back to the English
    title, i.e. today's behaviour)."""
    cache_key = f"tmdb:detitles:{tmdb_id}"
    cached = _local_get(cache_key)
    if cached is not None:
        return cached

    titles: List[str] = []
    seen: set = set()

    def _add(value: Optional[str]) -> None:
        value = (value or "").strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            titles.append(value)

    translations, alternatives = await asyncio.gather(
        fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}/translations"),
        fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}/alternative_titles"),
    )

    for t in ((translations or {}).get("translations") or []):
        if t.get("iso_639_1") == "de":
            _add((t.get("data") or {}).get("name"))
    for a in ((alternatives or {}).get("results") or []):
        if a.get("iso_3166_1") in ("DE", "AT", "CH"):
            _add(a.get("title"))

    _local_set(cache_key, titles, ttl=86400)
    return titles


# --- NON-ANIME TV SHOWS (secondary, additive) -------------------------------
# These mirror the anime discovery helpers above but invert the AniList gate:
# they surface TMDB TV results that are NOT mapped anime, so the site can also
# play general (non-anime) series through the same TMDB-keyed pipeline (/info,
# /watch/{tmdb_id}/{season}/{episode}) and the s.to scraper, which matches by
# title. Anime stays priority 1 — these are a separate, parallel surface and the
# anime helpers/endpoints above are left completely untouched.


def _looks_like_anime(item: Dict) -> bool:
    """Heuristic: a TMDB TV item that is Japanese Animation. Used to keep anime
    (including titles we haven't mapped in Fribb yet) out of the *shows* surface,
    so the two stay cleanly separated even at the edges."""
    genres = item.get("genre_ids") or []
    return 16 in genres and item.get("original_language") == "ja"


def _show_item(item: Dict) -> Dict:
    """Shape one TMDB TV search/discover result as a non-anime show entry. Keyed
    by tmdb_id (no anilist_id) and tagged ``kind: "show"`` so the frontend routes
    it through the TMDB-keyed show pages instead of the AniList ones."""
    return {
        "title": item.get("name") or item.get("original_name"),
        "tmdb_id": item.get("id"),
        "anilist_id": None,
        "kind": "show",
        "poster": _tmdb_img(item.get("poster_path")) if item.get("poster_path") else None,
        "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
        "vote_average": item.get("vote_average"),
    }


# --- GENERAL (NON-ANIME) MOVIES (secondary, additive) -----------------------
# The movie twin of the non-anime SHOWS surface above. Movies are a distinct TMDB
# entity (/movie/{id}, no seasons/episodes), so they get their own discovery
# helpers + a dedicated /watch/movie route. They are served by the TMDB-keyed
# sources (PlayIMDb, Cinema.bz, Movish, ShowBox, Jellyfin), whose resolvers already
# speak /movie/{tmdb}; the title-based anime scrapers are skipped for movies. Anime
# stays priority 1 and is left completely untouched.


def _looks_like_anime_movie(item: Dict) -> bool:
    """Heuristic twin of _looks_like_anime for movie results: Japanese Animation.
    Keeps anime films out of the general-movie surface."""
    genres = item.get("genre_ids") or []
    return 16 in genres and item.get("original_language") == "ja"


def _movie_item(item: Dict) -> Dict:
    """Shape one TMDB movie search/discover result as a general-movie entry. Keyed
    by tmdb_id (no anilist_id) and tagged ``kind: "movie"`` so the frontend routes
    it through the movie pages and the account layer namespaces its key."""
    return {
        "title": item.get("title") or item.get("original_title"),
        "tmdb_id": item.get("id"),
        "anilist_id": None,
        "kind": "movie",
        "poster": _tmdb_img(item.get("poster_path")) if item.get("poster_path") else None,
        "year": item.get("release_date", "")[:4] if item.get("release_date") else None,
        "vote_average": item.get("vote_average"),
    }
