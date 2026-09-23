"""
TMDB metadata fetchers.

All TMDB HTTP access: show, movie and season metadata, search, trending, the
genre map and localized titles. A pure remote-metadata concern that never touches
the mapping DB, so it imports only config, the HTTP client and the response cache.
"""

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

from core import single_flight
from core.http_client import http_client, fetch_with_retry
from core.response_cache import (
    CACHE_TTL,
    TRENDING_CACHE_TTL,
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


# Bump when the cached payload shape changes, so entries persisted across a
# deploy are ignored rather than served.
TMDB_CACHE_VERSION = "v3"


def _tmdb_img(path: Optional[str], size: str = "w500") -> Optional[str]:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


async def fetch_tmdb_genre_map(client: httpx.AsyncClient, kind: str) -> Dict[int, str]:
    """TMDB genre id to name map for ``kind``, cached.

    Discover and search results carry only genre ids. This turns them into the
    name lists stored alongside each row, so the recommend engine can score shows
    and movies the way it scores anime. Tiny and stable, so cached aggressively."""
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
            ttl_seconds=CACHE_TTL,
        )
        _local_set(cache_key, gmap)
    return gmap


async def fetch_tmdb_show(client: httpx.AsyncClient, tmdb_id: int,
                          force_refresh: bool = False) -> Dict:
    """A TMDB show with its real season list, the authority for what the TMDB-keyed
    sources can play. Cached, and persists core fields on first fetch.

    ``force_refresh`` skips the cache so the row is re-pulled and re-upserted,
    which is how the staleness refresher works.
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
        # Skip season 0 and empty placeholder seasons.
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
        # Stored so the recommend engine can score shows by genre too.
        "genres": [g.get("name") for g in (data.get("genres") or []) if g.get("name")],
        "popularity": data.get("popularity"),
        "seasons": seasons,
    }

    upsert_show_info({k: result.get(k) for k in
                      ("tmdb_id", "title", "overview", "poster_path", "backdrop_path", "first_air_date", "genres", "popularity")})
    await set_cached_response(cache_key, result)
    return result


async def fetch_tmdb_movie(client: httpx.AsyncClient, tmdb_id: int,
                           force_refresh: bool = False) -> Dict:
    """A TMDB movie, a different id space from /tv. Cached, and persists core
    fields on first fetch so the overview and watch pages degrade gracefully when
    TMDB is unavailable.

    Movies have no seasons, and the sources play them off the bare movie id, so
    this is all the metadata the movie surface needs."""
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
        "popularity": data.get("popularity"),
        "status": data.get("status"),
    }

    upsert_movie_info({k: result.get(k) for k in
                       ("tmdb_id", "title", "overview", "poster_path", "backdrop_path", "release_date",
                        "genres", "runtime", "vote_average", "popularity", "status", "original_title")})
    await set_cached_response(cache_key, result)
    return result


async def fetch_tmdb_metadata(client: httpx.AsyncClient, tmdb_id: int, season: int = 1,
                              show: Optional[Dict] = None) -> Dict:
    """Metadata and the episode list for one TMDB season.

    Falls back to the show-level overview when the season's is empty, which is
    common for anime, so a description is always available. ``show`` may be passed
    in by a caller that already fetched it, avoiding a redundant re-fetch.
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
    """Episode count and per-episode air dates for a TMDB season.

    Derived from the cached season metadata and also held in L1, since both the
    unaired gate and the progress enricher hit it on hot paths. Returns {} when
    the season cannot be loaded, so callers degrade to no gating."""
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
    """Search TMDB for anime titles."""
    cache_key = f"tmdb:search:{query.lower()}"

    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])

    async def _load() -> List[Dict]:
        url = "https://api.themoviedb.org/3/search/tv"
        data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})

        if not data:
            return []

        items = data.get("results", [])[:limit]
        # One batched lookup instead of a query per result.
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

        await set_cached_response(cache_key, {"results": results}, ttl_seconds=CACHE_TTL)
        return results

    # Coalesced: the client searches per keystroke, so one typed title arrives
    # as a burst of concurrent misses on the same key.
    return await single_flight.run(cache_key, _load)


async def fetch_trending_anime(client: httpx.AsyncClient, limit: int = 12) -> List[Dict]:
    """Trending anime from TMDB."""
    cache_key = "tmdb:trending"

    # L1, so a hit costs no DB round-trip.
    local = _local_get(cache_key)
    if local is not None:
        return local

    cached_data = await get_cached_response(cache_key)
    if cached_data:
        results = cached_data.get("results", [])
        _local_set(cache_key, results)
        return results

    async def _load() -> List[Dict]:
        url = "https://api.themoviedb.org/3/discover/tv"
        params = {
            "page": 1,
            "include_adult": "false",
            "language": "en-US",
            "with_genres": "16",             # Animation
            "with_original_language": "ja",  # Japanese originals
            "sort_by": "popularity.desc",
            "vote_count.gte": 100            # quality floor
        }

        data = await fetch_with_retry(client, url, params=params)

        if not data:
            return []

        items = data.get("results", [])[:limit]
        # One batched lookup instead of a query per result.
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

        # The DB for cross-replica reuse, L1 for this process.
        await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=TRENDING_CACHE_TTL)
        _local_set(cache_key, trending_list)
        return trending_list

    # Coalesced: one global key that every homepage load lands on at once.
    return await single_flight.run(cache_key, _load)


async def fetch_tmdb_show_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for general TV shows, excluding anime.

    Drops titles that already map to an AniList entry, which /search/anime serves,
    and anything that looks like Japanese animation, so unmapped anime cannot leak
    into the shows surface."""
    cache_key = f"tmdb:search_shows:{query.lower()}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])

    url = "https://api.themoviedb.org/3/search/tv"
    data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})
    if not data:
        return []

    items = data.get("results", [])
    # One batched lookup, to drop anything already mapped as anime.
    anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])
    genre_map = await fetch_tmdb_genre_map(client, "tv")

    results: List[Dict] = []
    for item in items:
        tmdb_id = item.get("id")
        if not tmdb_id or anilist_by_tmdb.get(tmdb_id) or _looks_like_anime(item):
            continue
        if not item.get("poster_path"):
            continue  # posterless rows are usually junk, and spoil the grid
        # Cached so it can seed recommendations later.
        _persist_discovered_show(item, genre_map)
        results.append(_show_item(item))
        if len(results) >= limit:
            break

    await set_cached_response(cache_key, {"results": results}, ttl_seconds=CACHE_TTL)
    return results


async def fetch_trending_shows(client: httpx.AsyncClient, limit: int = 10) -> List[Dict]:
    """Trending non-anime TV shows: popular, excluding animation."""
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
        "without_genres": "16",          # excludes Animation, keeping anime out
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
        # Popular shows make the best recommendation candidates.
        _persist_discovered_show(item, genre_map)
        trending_list.append(_show_item(item))
        if len(trending_list) >= limit:
            break

    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=TRENDING_CACHE_TTL)
    _local_set(cache_key, trending_list)
    return trending_list


async def fetch_tmdb_movie_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for general movies, excluding Japanese animation, which stays on
    the anime surface. Posterless rows are dropped."""
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

    await set_cached_response(cache_key, {"results": results}, ttl_seconds=CACHE_TTL)
    return results


async def fetch_trending_movies(client: httpx.AsyncClient, limit: int = 10) -> List[Dict]:
    """Trending general movies: popular, excluding animation."""
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
        "without_genres": "16",          # excludes Animation, keeping anime out
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

    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=TRENDING_CACHE_TTL)
    _local_set(cache_key, trending_list)
    return trending_list


# --- SCRAPER HELPERS --------------------------------------------------------
async def fetch_tmdb_localized_titles(client: httpx.AsyncClient, tmdb_id: int) -> List[str]:
    """German-language titles for a TMDB show, for the German scraper sites.

    Those sites list many shows under their German broadcast title rather than the
    English one TMDB hands over first, so plain title matching misses the show
    entirely. This pulls the localized name plus any DE/AT/CH alternative titles as
    extra search candidates. Cached, since they are stable, and an empty list on
    failure just falls matching back to the English title."""
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


async def fetch_tmdb_imdb_id(client: httpx.AsyncClient, tmdb_id: int,
                             media_type: str = "tv") -> Optional[str]:
    """The IMDb id for a TMDB show or movie, needed by the IMDb-keyed client
    sources. Cached, and None on failure so callers skip that source."""
    path = "movie" if media_type == "movie" else "tv"
    cache_key = f"tmdb:imdb:{path}:{tmdb_id}"
    cached = _local_get(cache_key)
    if cached is not None:
        return cached or None
    data = await fetch_with_retry(
        client, f"https://api.themoviedb.org/3/{path}/{tmdb_id}/external_ids"
    )
    imdb = ((data or {}).get("imdb_id") or "").strip()
    _local_set(cache_key, imdb, ttl=86400)
    return imdb or None


# --- NON-ANIME TV SHOWS -----------------------------------------------------
# These mirror the anime discovery helpers above but invert the AniList gate,
# surfacing TMDB TV results that are not mapped anime. The site plays them through
# the same TMDB-keyed pipeline and the title-matching scrapers. A separate,
# parallel surface: the anime helpers above are untouched.


def _looks_like_anime(item: Dict) -> bool:
    """Whether a TMDB TV item looks like Japanese animation. Keeps anime, including
    titles not yet mapped in Fribb, out of the shows surface."""
    genres = item.get("genre_ids") or []
    return 16 in genres and item.get("original_language") == "ja"


def _show_item(item: Dict) -> Dict:
    """Shape one TMDB TV result as a non-anime show entry, keyed by tmdb_id and
    tagged ``kind: "show"`` so the frontend routes it through the TMDB-keyed
    pages."""
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
