"""All TMDB HTTP access: show, movie and season metadata, search, trending, the
genre map, and the extra titles and ids some sources key on.

Full fetches are written through to ``store`` so the pages degrade gracefully
when TMDB is down, and non-anime search and discover results are kept there as
recommendation candidates. Anime results are the ones that map to an AniList id.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional

import httpx

from core import single_flight
from core.http_client import fetch_with_retry, http_client
from core.response_cache import (
    CACHE_TTL,
    TRENDING_CACHE_TTL,
    get_cached_response,
    local_get,
    local_set,
    set_cached_response,
)

from . import store

logger = logging.getLogger("crimson.tmdb")

# Bump when the cached payload shape changes, so entries persisted across a
# deploy are ignored rather than served.
TMDB_CACHE_VERSION = "v3"

ANIMATION_GENRE = 16


def tmdb_img(path: Optional[str], size: str = "w500") -> Optional[str]:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


def _year(date: Optional[str]) -> Optional[str]:
    return date[:4] if date else None


async def fetch_tmdb_genre_map(client: httpx.AsyncClient, kind: str) -> Dict[int, str]:
    """TMDB genre id -> name for ``kind`` ("tv" or "movie").

    Discover and search results carry only genre ids; the names are what gets
    stored, so the recommend engine can score shows and movies like anime.
    """
    if kind not in ("tv", "movie"):
        return {}
    cache_key = f"tmdb:genremap:{kind}"
    local = local_get(cache_key)
    if local is not None:
        return local
    cached = await get_cached_response(cache_key)
    if cached and "map" in cached:
        gmap = {int(k): v for k, v in cached["map"].items()}
        local_set(cache_key, gmap)
        return gmap

    data = await fetch_with_retry(
        client, f"https://api.themoviedb.org/3/genre/{kind}/list", params={"language": "en-US"}
    )
    gmap = {g["id"]: g["name"] for g in (data or {}).get("genres", []) if g.get("id") and g.get("name")}
    if gmap:
        await set_cached_response(cache_key, {"map": {str(k): v for k, v in gmap.items()}}, ttl_seconds=CACHE_TTL)
        local_set(cache_key, gmap)
    return gmap


async def fetch_tmdb_show(client: httpx.AsyncClient, tmdb_id: int, force_refresh: bool = False) -> Dict:
    """A TMDB show with its real season list, the authority for what the
    TMDB-keyed sources can play.

    ``force_refresh`` skips the cache so the row is re-pulled and re-stored,
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
        # Season 0 holds specials, and TMDB lists announced seasons with no episodes yet.
        if num is None or num < 1 or (s.get("episode_count") or 0) < 1:
            continue
        seasons.append({
            "season_number": num,
            "name": s.get("name") or f"Season {num}",
            "episode_count": s.get("episode_count"),
            "air_date": s.get("air_date"),
            "poster": tmdb_img(s.get("poster_path")),
            "overview": s.get("overview"),
        })

    result = {
        "tmdb_id": tmdb_id,
        "title": data.get("name") or data.get("original_name"),
        "overview": data.get("overview"),
        "poster_path": data.get("poster_path"),
        "backdrop_path": data.get("backdrop_path"),
        "poster": tmdb_img(data.get("poster_path")),
        "backdrop": tmdb_img(data.get("backdrop_path"), "original"),
        "first_air_date": data.get("first_air_date"),
        "genres": [g.get("name") for g in (data.get("genres") or []) if g.get("name")],
        "popularity": data.get("popularity"),
        "seasons": seasons,
    }

    await asyncio.to_thread(store.upsert_shows, [result])
    await set_cached_response(cache_key, result)
    return result


async def fetch_tmdb_movie(client: httpx.AsyncClient, tmdb_id: int, force_refresh: bool = False) -> Dict:
    """A TMDB movie (a separate id space from /tv). Movies have no seasons and
    the sources play them off the bare movie id, so this is all the metadata the
    movie pages need."""
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
        "poster": tmdb_img(data.get("poster_path")),
        "backdrop": tmdb_img(data.get("backdrop_path"), "original"),
        "release_date": data.get("release_date"),
        "original_title": data.get("original_title"),
        "runtime": data.get("runtime"),
        "genres": [g.get("name") for g in (data.get("genres") or []) if g.get("name")],
        "vote_average": data.get("vote_average"),
        "popularity": data.get("popularity"),
        "status": data.get("status"),
    }

    await asyncio.to_thread(store.upsert_movies, [result])
    await set_cached_response(cache_key, result)
    return result


async def fetch_tmdb_metadata(client: httpx.AsyncClient, tmdb_id: int, season: int = 1,
                              show: Optional[Dict] = None) -> Dict:
    """Metadata and the episode list for one TMDB season.

    Anime seasons often have no overview of their own, so the show's stands in.
    A caller that already holds ``show`` passes it to save the lookup.
    """
    cache_key = f"tmdb:meta:{TMDB_CACHE_VERSION}:{tmdb_id}:s{season}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    data = await fetch_with_retry(client, f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}")
    if show is None:
        show = await fetch_tmdb_show(client, tmdb_id)

    result: Dict
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
            "thumbnail": tmdb_img(ep.get("still_path")),
            "overview": ep.get("overview"),
            "air_date": ep.get("air_date"),
            "url": None,
        } for ep in data.get("episodes", [])]

        result = {
            "summary": data.get("overview") or show.get("overview"),
            "poster": tmdb_img(data.get("poster_path")) or show.get("poster"),
            "backdrop": show.get("backdrop"),
            "season_name": data.get("name") or f"Season {season}",
            "air_date": data.get("air_date"),
            "episodes": episodes,
        }

    await set_cached_response(cache_key, result)
    return result


async def season_episode_info(tmdb_id: int, season_number: int) -> Dict:
    """Episode count and per-episode air dates for a TMDB season, or {} when the
    season cannot be loaded (callers then skip the unaired gate). Held in L1 too,
    since the unaired gate and the progress enricher hit it on hot paths."""
    key = f"epinfo:{tmdb_id}:s{season_number}"
    cached = local_get(key)
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
    local_set(key, info)
    return info


# Search and trending cache the whole filtered first page under a key without the
# limit and slice on return, so whichever caller misses first cannot decide how
# many results everyone else gets until expiry.


async def _cached_results(cache_key: str, ttl: int,
                          load: Callable[[], Awaitable[Optional[List[Dict]]]]) -> List[Dict]:
    """``load`` returns None when TMDB failed, which is not cached."""
    local = local_get(cache_key)
    if local is not None:
        return local
    cached = await get_cached_response(cache_key)
    if cached:
        results = cached.get("results", [])
        local_set(cache_key, results)
        return results

    async def _load_and_store() -> List[Dict]:
        results = await load()
        if results is None:
            return []
        await set_cached_response(cache_key, {"results": results}, ttl_seconds=ttl)
        local_set(cache_key, results)
        return results

    # Coalesced: search runs per keystroke and trending is one key every homepage
    # load lands on, so both arrive as bursts of concurrent misses.
    return await single_flight.run(cache_key, _load_and_store)


def _looks_like_anime(item: Dict) -> bool:
    """Japanese animation, mapped or not, belongs on the anime surface only."""
    return ANIMATION_GENRE in (item.get("genre_ids") or []) and item.get("original_language") == "ja"


async def keep_non_anime(client: httpx.AsyncClient, kind: str, items: List[Dict]) -> List[Dict]:
    """The postered, non-anime ``items`` of a TMDB tv or movie result page, stored
    as recommendation candidates on the way through. For tv, anything already
    mapped to AniList is anime too, whatever its genres say."""
    candidates = [it for it in items if it.get("id") and it.get("poster_path") and not _looks_like_anime(it)]
    if kind == "tv":
        mapped = await asyncio.to_thread(store.get_first_anilist_ids, [it["id"] for it in candidates])
        candidates = [it for it in candidates if it["id"] not in mapped]
    if not candidates:
        return []
    genre_map = await fetch_tmdb_genre_map(client, kind)
    persist = store.persist_discovered_shows if kind == "tv" else store.persist_discovered_movies
    await asyncio.to_thread(persist, candidates, genre_map)
    return candidates


async def _mapped_anime(items: List[Dict]) -> List[Dict]:
    anilist_by_tmdb = await asyncio.to_thread(
        store.get_first_anilist_ids, [it["id"] for it in items if it.get("id")]
    )
    return [
        {
            "title": item.get("name") or item.get("original_name"),
            "tmdb_id": item["id"],
            "anilist_id": anilist_by_tmdb[item["id"]],
            "poster": tmdb_img(item.get("poster_path")),
            "year": _year(item.get("first_air_date")),
            "vote_average": item.get("vote_average"),
        }
        for item in items
        if item.get("id") in anilist_by_tmdb
    ]


def _show_item(item: Dict) -> Dict:
    """``kind: "show"`` routes it through the TMDB-keyed pages on the client."""
    return {
        "title": item.get("name") or item.get("original_name"),
        "tmdb_id": item.get("id"),
        "anilist_id": None,
        "kind": "show",
        "poster": tmdb_img(item.get("poster_path")),
        "year": _year(item.get("first_air_date")),
        "vote_average": item.get("vote_average"),
    }


def _movie_item(item: Dict) -> Dict:
    """``kind: "movie"`` routes it through the movie pages and namespaces its
    account keys."""
    return {
        "title": item.get("title") or item.get("original_title"),
        "tmdb_id": item.get("id"),
        "anilist_id": None,
        "kind": "movie",
        "poster": tmdb_img(item.get("poster_path")),
        "year": _year(item.get("release_date")),
        "vote_average": item.get("vote_average"),
    }


def discover_params(kind: str) -> Dict:
    """Popular non-animation titles, with a vote floor that keeps obscure junk out."""
    return {
        "page": 1,
        "include_adult": "false",
        "language": "en-US",
        "without_genres": str(ANIMATION_GENRE),
        "sort_by": "popularity.desc",
        "vote_count.gte": 200 if kind == "tv" else 300,
    }


async def fetch_tmdb_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """TMDB tv search, keeping only titles mapped to AniList."""
    async def _load() -> Optional[List[Dict]]:
        data = await fetch_with_retry(
            client, "https://api.themoviedb.org/3/search/tv", params={"query": query, "include_adult": "false"}
        )
        return await _mapped_anime(data.get("results", [])) if data else None

    results = await _cached_results(f"tmdb:search:{query.lower()}", CACHE_TTL, _load)
    return results[:limit]


async def fetch_trending_anime(client: httpx.AsyncClient, limit: int = 12) -> List[Dict]:
    async def _load() -> Optional[List[Dict]]:
        params = {
            "page": 1,
            "include_adult": "false",
            "language": "en-US",
            "with_genres": str(ANIMATION_GENRE),
            "with_original_language": "ja",
            "sort_by": "popularity.desc",
            "vote_count.gte": 100,
        }
        data = await fetch_with_retry(client, "https://api.themoviedb.org/3/discover/tv", params=params)
        return await _mapped_anime(data.get("results", [])) if data else None

    results = await _cached_results("tmdb:trending", TRENDING_CACHE_TTL, _load)
    return results[:limit]


async def fetch_tmdb_show_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """TMDB tv search for everything that is not anime; /search/anime serves that."""
    async def _load() -> Optional[List[Dict]]:
        data = await fetch_with_retry(
            client, "https://api.themoviedb.org/3/search/tv", params={"query": query, "include_adult": "false"}
        )
        if not data:
            return None
        return [_show_item(it) for it in await keep_non_anime(client, "tv", data.get("results", []))]

    results = await _cached_results(f"tmdb:search_shows:{query.lower()}", CACHE_TTL, _load)
    return results[:limit]


async def fetch_trending_shows(client: httpx.AsyncClient, limit: int = 10) -> List[Dict]:
    async def _load() -> Optional[List[Dict]]:
        data = await fetch_with_retry(client, "https://api.themoviedb.org/3/discover/tv", params=discover_params("tv"))
        if not data:
            return None
        return [_show_item(it) for it in await keep_non_anime(client, "tv", data.get("results", []))]

    results = await _cached_results("tmdb:trending_shows", TRENDING_CACHE_TTL, _load)
    return results[:limit]


async def fetch_tmdb_movie_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """TMDB movie search without Japanese animation, which stays on the anime surface."""
    async def _load() -> Optional[List[Dict]]:
        data = await fetch_with_retry(
            client, "https://api.themoviedb.org/3/search/movie", params={"query": query, "include_adult": "false"}
        )
        if not data:
            return None
        return [_movie_item(it) for it in await keep_non_anime(client, "movie", data.get("results", []))]

    results = await _cached_results(f"tmdb:search_movies:{query.lower()}", CACHE_TTL, _load)
    return results[:limit]


async def fetch_trending_movies(client: httpx.AsyncClient, limit: int = 10) -> List[Dict]:
    async def _load() -> Optional[List[Dict]]:
        data = await fetch_with_retry(
            client, "https://api.themoviedb.org/3/discover/movie", params=discover_params("movie")
        )
        if not data:
            return None
        return [_movie_item(it) for it in await keep_non_anime(client, "movie", data.get("results", []))]

    results = await _cached_results("tmdb:trending_movies", TRENDING_CACHE_TTL, _load)
    return results[:limit]


async def fetch_tmdb_localized_titles(client: httpx.AsyncClient, tmdb_id: int) -> List[str]:
    """German titles for a TMDB show, as extra search candidates for the German
    scraper sites, which list many shows under their German broadcast title. An
    empty list on failure just leaves matching on the English title."""
    cache_key = f"tmdb:detitles:{tmdb_id}"
    cached = local_get(cache_key)
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

    local_set(cache_key, titles, ttl=86400)
    return titles


async def fetch_tmdb_imdb_id(client: httpx.AsyncClient, tmdb_id: int, media_type: str = "tv") -> Optional[str]:
    """The IMDb id the IMDb-keyed sources need, or None so callers skip them."""
    path = "movie" if media_type == "movie" else "tv"
    cache_key = f"tmdb:imdb:{path}:{tmdb_id}"
    cached = local_get(cache_key)
    if cached is not None:
        return cached or None
    data = await fetch_with_retry(client, f"https://api.themoviedb.org/3/{path}/{tmdb_id}/external_ids")
    imdb = ((data or {}).get("imdb_id") or "").strip()
    local_set(cache_key, imdb, ttl=86400)
    return imdb or None
