"""
AniList metadata fetcher, lifted out of api.py.

GraphQL fetch of a title's AniList metadata (titles, synonyms, episodes, airing
info), plus the tiny ``_empty`` coroutine api.py uses to gather an optional
AniList fetch without branching.
"""

import asyncio
import logging
from typing import Dict, Optional

import httpx

from core.config import Config
from core.response_cache import (
    _local_get,
    _local_set,
    get_cached_response,
    get_stale_response,
    set_cached_response_shadowed,
)

logger = logging.getLogger("crimson.anilist")

ANILIST_URL = "https://graphql.anilist.co"
# Cap how long a single web request will block on an AniList Retry-After. AniList
# can ask for 60s+ when rate-limiting; we won't hang a user request that long —
# past this ceiling we give up and let the caller degrade (serve-stale / empty).
_MAX_RETRY_WAIT = 8.0


async def _empty() -> Dict:
    """A coroutine that resolves to ``{}`` — lets us ``asyncio.gather`` an
    optional fetch (e.g. AniList when there's no mapping) without branching."""
    return {}


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    """Parse a Retry-After header (delta-seconds form, which AniList uses)."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


async def anilist_post(
    client: httpx.AsyncClient,
    query: str,
    variables: Optional[Dict] = None,
    *,
    timeout: Optional[float] = None,
) -> Optional[httpx.Response]:
    """POST a GraphQL query to AniList with retry + backoff.

    AniList is frequently rate-limited (a degraded ~30 req/min ceiling → HTTP 429)
    or transiently 5xx; a one-shot POST turns that blip into a hard failure and, on
    the discovery hubs, a 503 or an empty grid. This retries 429 (honoring
    Retry-After, capped at ``_MAX_RETRY_WAIT`` so a request never hangs) and
    500/502/503/504 with exponential backoff, and retries network/timeout errors.

    Returns the final ``httpx.Response`` — the successful one, or the last failing
    one once retries are exhausted — so callers keep their existing status-code and
    GraphQL-``errors[]`` handling unchanged. Re-raises the last network exception
    only if no attempt ever produced a response.
    """
    payload: Dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    timeout = timeout or Config.REQUEST_TIMEOUT

    response: Optional[httpx.Response] = None
    for attempt in range(Config.MAX_RETRIES):
        last = attempt == Config.MAX_RETRIES - 1
        try:
            response = await client.post(ANILIST_URL, json=payload, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if last:
                raise
            logger.warning(
                f"AniList request error ({type(e).__name__}); retry {attempt + 1}/{Config.MAX_RETRIES}"
            )
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
            continue

        if response.status_code == 429:
            if last:
                return response
            wait = _retry_after_seconds(response)
            wait = min(wait, _MAX_RETRY_WAIT) if wait is not None else Config.RETRY_BACKOFF_FACTOR * (2 ** attempt)
            logger.warning(
                f"AniList rate limited (429); waiting {wait}s before retry {attempt + 1}/{Config.MAX_RETRIES}"
            )
            await asyncio.sleep(wait)
            continue

        if response.status_code in (500, 502, 503, 504):
            if last:
                return response
            logger.warning(
                f"AniList upstream {response.status_code}; retry {attempt + 1}/{Config.MAX_RETRIES}"
            )
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
            continue

        return response

    return response


async def fetch_anilist_metadata(client: httpx.AsyncClient, anilist_id: int) -> Dict:
    """Fetch anime metadata from AniList"""
    cache_key = f"anilist:meta:{anilist_id}"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data
    
    query = """
    query ($id: Int) {
      Media (id: $id, type: ANIME) {
        id
        idMal
        status
        episodes
        bannerImage
        coverImage {
          large
          extraLarge
        }
        title {
          romaji
          english
          native
        }
        synonyms
        description
        startDate {
          year
          month
          day
        }
        endDate {
          year
          month
          day
        }
        streamingEpisodes {
          title
          thumbnail
          url
        }
        nextAiringEpisode {
          episode
          airingAt
        }
      }
    }
    """
    
    try:
        response = await anilist_post(client, query, {"id": anilist_id})

        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            logger.error(f"AniList API error: Status {status}")
            # Outage → serve the last known good copy rather than a blank {} (which
            # would 404 the overview / drop metadata from the watch pipeline).
            return await get_stale_response(cache_key) or {}

        data = response.json()
        media = data.get("data", {}).get("Media", {})
        if not media: return {}
        
        # Format streaming episodes
        raw_episodes = media.get("streamingEpisodes", [])
        formatted_episodes = []
        
        for index, ep in enumerate(raw_episodes, start=1):
            formatted_episodes.append({
                "episode_number": index,
                "title": ep.get("title", f"Episode {index}"),
                "thumbnail": ep.get("thumbnail"),
                "url": ep.get("url")
            })
        
        # Fallback to generated episode list if no streaming episodes
        if not formatted_episodes and media.get("episodes"):
            total_episodes = media.get("episodes")
            for i in range(1, total_episodes + 1):
                formatted_episodes.append({
                    "episode_number": i,
                    "title": f"Episode {i}",
                    "thumbnail": None,
                    "url": None
                })
        
        result = {
            "anilist_id": media.get("id"),
            # MyAnimeList id (AniList's idMal). Surfaced so the skip-intro feature
            # can key AniSkip off it (see skiptimes_engine); additive field.
            "mal_id": media.get("idMal"),
            "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
            "title_romaji": media.get("title", {}).get("romaji"),
            "title_english": media.get("title", {}).get("english"),
            "title_native": media.get("title", {}).get("native"),
            "synonyms": media.get("synonyms") or [],
            "total_episodes": media.get("episodes"),
            "status": media.get("status"),
            "banner": media.get("bannerImage"),
            "cover": media.get("coverImage", {}).get("extraLarge") or media.get("coverImage", {}).get("large"),
            "description": media.get("description"),
            "start_date": media.get("startDate"),
            "end_date": media.get("endDate"),
            "next_airing_episode": media.get("nextAiringEpisode"),
            "episodes_list": formatted_episodes
        }
        
        # Cache the result (+ a long-lived shadow for serve-stale-on-error).
        if result:
            await set_cached_response_shadowed(cache_key, result, ttl_seconds=Config.CACHE_TTL_SECONDS)

        return result

    except Exception as e:
        logger.error(f"Error fetching from AniList: {e}")
        return await get_stale_response(cache_key) or {}


# --- MANGA (the reading surface) -------------------------------------------
# AniList's ``MediaType`` already includes ``MANGA``, so the manga surface reuses
# the exact same GraphQL endpoint + response cache as the anime metadata above —
# only ``type: MANGA`` and the manga-specific fields (chapters/volumes instead of
# episodes/airing) differ. Kept here beside the anime fetcher so all AniList logic
# lives in one place. See manga_engine for how these feed the /manga routes.

def _manga_item(media: Dict) -> Dict:
    """Project one AniList MANGA ``Media`` node onto the poster-card shape the
    frontend rows + unified search consume (``kind: 'manga'``)."""
    title = media.get("title") or {}
    cover = media.get("coverImage") or {}
    score = media.get("averageScore")
    return {
        "anilist_id": media.get("id"),
        "title": title.get("english") or title.get("romaji") or title.get("native"),
        "poster": cover.get("extraLarge") or cover.get("large"),
        "year": (media.get("startDate") or {}).get("year"),
        # AniList scores are 0-100; the card renders a 0-10 rating.
        "vote_average": (score / 10.0) if isinstance(score, (int, float)) and score else None,
        "kind": "manga",
    }


async def fetch_anilist_manga_metadata(client: httpx.AsyncClient, anilist_id: int) -> Dict:
    """Full metadata for a single AniList MANGA entry (the manga overview page).

    Cached like the anime fetcher. Returns ``{}`` on any miss/failure so a caller
    can degrade gracefully."""
    cache_key = f"anilist:manga:meta:{anilist_id}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    query = """
    query ($id: Int) {
      Media (id: $id, type: MANGA) {
        id
        idMal
        status
        chapters
        volumes
        bannerImage
        coverImage { large extraLarge color }
        title { romaji english native }
        synonyms
        genres
        description
        averageScore
        startDate { year month day }
        endDate { year month day }
      }
    }
    """
    try:
        response = await anilist_post(client, query, {"id": anilist_id})
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            logger.error(f"AniList manga API error: Status {status}")
            return await get_stale_response(cache_key) or {}
        media = (response.json().get("data") or {}).get("Media") or {}
        if not media:
            return await get_stale_response(cache_key) or {}

        title = media.get("title") or {}
        cover = media.get("coverImage") or {}
        result = {
            "anilist_id": media.get("id"),
            "mal_id": media.get("idMal"),
            "title": title.get("english") or title.get("romaji"),
            "title_romaji": title.get("romaji"),
            "title_english": title.get("english"),
            "title_native": title.get("native"),
            "synonyms": media.get("synonyms") or [],
            "genres": media.get("genres") or [],
            "status": media.get("status"),
            "chapters_total": media.get("chapters"),
            "volumes_total": media.get("volumes"),
            "banner": media.get("bannerImage"),
            "cover": cover.get("extraLarge") or cover.get("large"),
            "color": cover.get("color"),
            "poster": cover.get("extraLarge") or cover.get("large"),
            "description": media.get("description"),
            "average_score": media.get("averageScore"),
            "start_date": media.get("startDate"),
            "end_date": media.get("endDate"),
        }
        await set_cached_response_shadowed(cache_key, result, ttl_seconds=Config.CACHE_TTL_SECONDS)
        return result
    except Exception as e:
        logger.error(f"Error fetching manga from AniList: {e}")
        return await get_stale_response(cache_key) or {}


async def search_anilist_manga(client: httpx.AsyncClient, query_name: str, per_page: int = 12) -> list:
    """AniList MANGA search for the unified landing search — returns a list of
    poster-card items (``kind: 'manga'``). Non-adult only by default."""
    graphql = """
    query ($search: String, $perPage: Int) {
      Page (page: 1, perPage: $perPage) {
        media (search: $search, type: MANGA, isAdult: false, sort: SEARCH_MATCH) {
          id
          title { romaji english native }
          coverImage { large extraLarge }
          startDate { year }
          averageScore
        }
      }
    }
    """
    try:
        response = await anilist_post(client, graphql, {"search": query_name, "perPage": per_page})
        if response is None or response.status_code != 200:
            return []
        media = ((response.json().get("data") or {}).get("Page") or {}).get("media") or []
        return [_manga_item(m) for m in media if m.get("id")]
    except Exception as e:
        logger.error(f"Error searching manga on AniList: {e}")
        return []


async def fetch_trending_manga(client: httpx.AsyncClient, limit: int = 12) -> dict:
    """Trending AniList MANGA for the landing page's manga row.

    Returns ``{"items": [poster-cards], "stale": bool}``. Cached (the list is
    identical for every viewer within the window); on an AniList outage it serves
    the last known good copy tagged ``stale: True`` instead of an empty row.
    """
    cache_key = f"anilist:manga:trending:{limit}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return {"items": cached_data, "stale": False}

    graphql = """
    query ($perPage: Int) {
      Page (page: 1, perPage: $perPage) {
        media (type: MANGA, isAdult: false, sort: TRENDING_DESC) {
          id
          title { romaji english native }
          coverImage { large extraLarge }
          startDate { year }
          averageScore
        }
      }
    }
    """
    result: list = []
    try:
        response = await anilist_post(client, graphql, {"perPage": limit})
        if response is not None and response.status_code == 200:
            media = ((response.json().get("data") or {}).get("Page") or {}).get("media") or []
            result = [_manga_item(m) for m in media if m.get("id")]
    except Exception as e:
        logger.error(f"Error fetching trending manga from AniList: {e}")

    if result:
        await set_cached_response_shadowed(
            cache_key, result, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS
        )
        return {"items": result, "stale": False}

    # Live fetch failed/empty → serve the last known good row if we have one.
    stale = await get_stale_response(cache_key)
    if stale:
        return {"items": stale, "stale": True}
    return {"items": [], "stale": False}


# --- Manga browse hub (live AniList; no DB table, so this cannot be local) ----
# The manga twin of /catalogue/shows|movies, but paginated + live: there is no
# manga table (see manga_engine docstring), so a genre/sort browse must hit
# AniList directly. Cached per (genre, sort, page) in the response cache like
# fetch_trending_manga. The frontend Manga hub drives page/sort/genre and appends
# pages ("load more"), since the full corpus is far too large to ship at once.

# The anime browse hub shares this exact machinery (only ``type: ANIME`` differs):
# the anime /catalogue is 6,800 mapped titles and slow to ship+render whole, so the
# DEFAULT anime browse is this same fast, paginated, poster-rich AniList grid; the
# full local catalogue stays a secondary "Archive" view.

# Friendly sort token -> AniList MediaSort enum. Trending is the default browse
# order (matches the trending row); the rest give the hub its sort control. Shared
# by the anime + manga catalogue browses (MediaSort applies to both media types).
_MEDIA_SORTS = {
    "trending": "TRENDING_DESC",
    "popular": "POPULARITY_DESC",
    "score": "SCORE_DESC",
    "newest": "START_DATE_DESC",
    "title": "TITLE_ROMAJI",
}
CATALOGUE_DEFAULT_SORT = "trending"
# Back-compat alias (manga_engine + older imports referenced MANGA_DEFAULT_SORT).
MANGA_DEFAULT_SORT = CATALOGUE_DEFAULT_SORT


async def fetch_anilist_genres(client: httpx.AsyncClient) -> list:
    """AniList's genre vocabulary (shared anime/manga) for the browse hubs' filter
    chips. Tiny and very stable, so cached aggressively (L1 + response cache)."""
    cache_key = "anilist:genres"
    local = _local_get(cache_key)
    if local is not None:
        return local
    cached = await get_cached_response(cache_key)
    if cached and "genres" in cached:
        _local_set(cache_key, cached["genres"])
        return cached["genres"]
    query = "query { GenreCollection }"

    async def _stale_genres() -> list:
        """Last known good genre vocabulary (chips still render during an outage)."""
        stale = await get_stale_response(cache_key)
        if stale and stale.get("genres"):
            _local_set(cache_key, stale["genres"])
            return stale["genres"]
        return []

    try:
        response = await anilist_post(client, query)
        if response is None or response.status_code != 200:
            return await _stale_genres()
        genres = (response.json().get("data") or {}).get("GenreCollection") or []
        if genres:
            await set_cached_response_shadowed(cache_key, {"genres": genres}, ttl_seconds=Config.CACHE_TTL_SECONDS)
            _local_set(cache_key, genres)
            return genres
        return await _stale_genres()
    except Exception as e:
        logger.error(f"Error fetching AniList genres: {e}")
        return await _stale_genres()


async def _fetch_media_catalogue(
    client: httpx.AsyncClient,
    media_type: str,
    kind: str,
    genre: Optional[str],
    sort: str,
    page: int,
    per_page: int,
) -> Dict:
    """One page of an AniList browse hub for ``media_type`` ('ANIME' | 'MANGA').

    ``Page.media(type: …)`` with an optional ``genre`` filter and a friendly
    ``sort`` token; returns ``{items, page, has_next, total}`` where ``items`` are
    poster cards tagged ``kind`` (so anime routes to /anime/{id}, manga to
    /manga/{id}). Cached per (media_type, genre, sort, page)."""
    sort_enum = _MEDIA_SORTS.get(sort, _MEDIA_SORTS[CATALOGUE_DEFAULT_SORT])
    page = max(1, page)
    genre_key = (genre or "").casefold()
    cache_key = f"anilist:{media_type.lower()}:browse:{genre_key}:{sort_enum}:{page}:{per_page}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    graphql = """
    query ($page: Int, $perPage: Int, $type: MediaType, $sort: [MediaSort], $genre: String) {
      Page (page: $page, perPage: $perPage) {
        pageInfo { hasNextPage total currentPage lastPage }
        media (type: $type, isAdult: false, sort: $sort, genre: $genre) {
          id
          title { romaji english native }
          coverImage { large extraLarge }
          startDate { year }
          averageScore
        }
      }
    }
    """
    variables = {"page": page, "perPage": per_page, "type": media_type, "sort": [sort_enum]}
    if genre:
        variables["genre"] = genre

    # An upstream failure is distinct from a genuinely empty page. Rather than the
    # bare "temporarily unavailable", first try to serve the last known good copy of
    # THIS exact page (tagged `stale: True`); only if no shadow exists do we return
    # `unavailable`, which the caller turns into a 503 (manga) or the local-DB
    # fallback (anime). Never caches the failure, so it self-heals on retry.
    async def _unavailable_or_stale() -> Dict:
        shadow = await get_stale_response(cache_key)
        if shadow:
            out = dict(shadow)
            out["stale"] = True
            out.pop("unavailable", None)
            return out
        return {"items": [], "page": page, "has_next": False, "total": 0, "unavailable": True}

    try:
        response = await anilist_post(client, graphql, variables)
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            logger.error(f"AniList {kind} browse error: Status {status}")
            return await _unavailable_or_stale()
        payload = response.json()
        # AniList returns HTTP 200 even on failure, with the real error in `errors`
        # (e.g. the whole API being disabled). Treat that as unavailable, not empty —
        # and LOG it, so an outage leaves a breadcrumb instead of a silent blank grid.
        if payload.get("errors"):
            msg = (payload["errors"][0] or {}).get("message", "unknown error")
            logger.warning(f"AniList {kind} browse GraphQL error: {msg}")
            return await _unavailable_or_stale()
        page_data = ((payload.get("data") or {}).get("Page") or {})
        info = page_data.get("pageInfo") or {}
        media = page_data.get("media") or []
        # _manga_item is the generic AniList-media projection; only the `kind` tag
        # differs between anime and manga, so re-tag it for anime.
        items = []
        for m in media:
            if not m.get("id"):
                continue
            item = _manga_item(m)
            item["kind"] = kind
            items.append(item)
        result = {
            "items": items,
            "page": info.get("currentPage") or page,
            "has_next": bool(info.get("hasNextPage")),
            "total": info.get("total") or 0,
        }
        if result["items"]:
            await set_cached_response_shadowed(
                cache_key, result, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS
            )
        return result
    except Exception as e:
        logger.error(f"Error fetching {kind} catalogue from AniList: {e}")
        return await _unavailable_or_stale()


async def fetch_manga_catalogue(
    client: httpx.AsyncClient,
    genre: Optional[str] = None,
    sort: str = CATALOGUE_DEFAULT_SORT,
    page: int = 1,
    per_page: int = 30,
) -> Dict:
    """One page of the manga browse hub — see _fetch_media_catalogue."""
    return await _fetch_media_catalogue(client, "MANGA", "manga", genre, sort, page, per_page)


async def fetch_anime_catalogue(
    client: httpx.AsyncClient,
    genre: Optional[str] = None,
    sort: str = CATALOGUE_DEFAULT_SORT,
    page: int = 1,
    per_page: int = 30,
) -> Dict:
    """One page of the anime browse hub (the fast default view) — the anime twin of
    fetch_manga_catalogue. Items are ``kind: 'anime'`` poster cards keyed by
    anilist_id, so they route through the existing /anime/{anilist_id} pages."""
    return await _fetch_media_catalogue(client, "ANIME", "anime", genre, sort, page, per_page)
