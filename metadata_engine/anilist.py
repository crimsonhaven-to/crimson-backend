"""All AniList GraphQL access: one title's metadata, the anime and manga browse
hubs, and the manga search, trending row and overview (AniList is the only manga
source).

Every fetch serves the last known good copy (the response cache's stale shadow)
when AniList fails, because it rate-limits hard and an empty grid or a 404'd
overview is worse than slightly old data.
"""

import asyncio
import logging
from typing import Dict, Optional

import httpx

from core import single_flight
from core.http_client import MAX_RETRIES, REQUEST_TIMEOUT, RETRY_BACKOFF
from core.response_cache import (
    CACHE_TTL,
    TRENDING_CACHE_TTL,
    local_get,
    local_set,
    get_cached_response,
    get_stale_response,
    set_cached_response_shadowed,
)

logger = logging.getLogger("crimson.anilist")

ANILIST_URL = "https://graphql.anilist.co"
# AniList can ask for 60s+ when rate-limiting, which is far too long to hang a
# user request on. Past this ceiling the caller degrades instead.
_MAX_RETRY_WAIT = 8.0


async def empty() -> Dict:
    """Resolves to ``{}``, so an optional fetch can be gathered without branching."""
    return {}


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    # AniList sends the delta-seconds form only.
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
    """POST a GraphQL query, retrying 429 (honouring Retry-After up to
    ``_MAX_RETRY_WAIT``) and 5xx with exponential backoff.

    Returns the final response, successful or not, so callers keep their own
    status and ``errors[]`` handling. Raises the last network error only when no
    attempt produced a response.
    """
    payload: Dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    timeout = timeout or REQUEST_TIMEOUT

    response: Optional[httpx.Response] = None
    for attempt in range(MAX_RETRIES):
        last = attempt == MAX_RETRIES - 1
        try:
            response = await client.post(ANILIST_URL, json=payload, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if last:
                raise
            logger.warning(
                f"AniList request error ({type(e).__name__}); retry {attempt + 1}/{MAX_RETRIES}"
            )
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
            continue

        if response.status_code == 429:
            if last:
                return response
            wait = _retry_after_seconds(response)
            wait = min(wait, _MAX_RETRY_WAIT) if wait is not None else RETRY_BACKOFF * (2 ** attempt)
            logger.warning(
                f"AniList rate limited (429); waiting {wait}s before retry {attempt + 1}/{MAX_RETRIES}"
            )
            await asyncio.sleep(wait)
            continue

        if response.status_code in (500, 502, 503, 504):
            if last:
                return response
            logger.warning(
                f"AniList upstream {response.status_code}; retry {attempt + 1}/{MAX_RETRIES}"
            )
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
            continue

        return response

    return response


async def fetch_anilist_metadata(client: httpx.AsyncClient, anilist_id: int) -> Dict:
    """Titles, synonyms, episodes and airing info for one AniList id, or {}."""
    cache_key = f"anilist:meta:{anilist_id}"

    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    async def _load() -> Dict:
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
                return await get_stale_response(cache_key) or {}

            media = (response.json().get("data") or {}).get("Media") or {}
            if not media:
                return await get_stale_response(cache_key) or {}

            episodes = [
                {
                    "episode_number": index,
                    "title": ep.get("title", f"Episode {index}"),
                    "thumbnail": ep.get("thumbnail"),
                    "url": ep.get("url"),
                }
                for index, ep in enumerate(media.get("streamingEpisodes") or [], start=1)
            ]
            if not episodes and media.get("episodes"):
                episodes = [
                    {"episode_number": i, "title": f"Episode {i}", "thumbnail": None, "url": None}
                    for i in range(1, media["episodes"] + 1)
                ]

            title = media.get("title") or {}
            cover = media.get("coverImage") or {}

            result = {
                "anilist_id": media.get("id"),
                # Surfaced so the skip-intro feature can key AniSkip off it.
                "mal_id": media.get("idMal"),
                "title": title.get("english") or title.get("romaji"),
                "title_romaji": title.get("romaji"),
                "title_english": title.get("english"),
                "title_native": title.get("native"),
                "synonyms": media.get("synonyms") or [],
                "total_episodes": media.get("episodes"),
                "status": media.get("status"),
                "banner": media.get("bannerImage"),
                "cover": cover.get("extraLarge") or cover.get("large"),
                "description": media.get("description"),
                "start_date": media.get("startDate"),
                "end_date": media.get("endDate"),
                "next_airing_episode": media.get("nextAiringEpisode"),
                "episodes_list": episodes,
            }
            await set_cached_response_shadowed(cache_key, result, ttl_seconds=CACHE_TTL)
            return result

        except Exception as e:
            logger.error(f"Error fetching from AniList: {e}")
            return await get_stale_response(cache_key) or {}

    # Coalesced: a popular title's entry expires while that title is at peak
    # traffic, and AniList answers a stampede with 429s.
    return await single_flight.run(cache_key, _load)


def media_card(media: Dict, kind: str = "manga") -> Dict:
    """An AniList ``Media`` node as the client's poster card, tagged ``kind`` so
    it routes to its own pages."""
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
        "kind": kind,
    }


async def fetch_anilist_manga_metadata(client: httpx.AsyncClient, anilist_id: int) -> Dict:
    """One manga's overview metadata, or {}."""
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
        await set_cached_response_shadowed(cache_key, result, ttl_seconds=CACHE_TTL)
        return result
    except Exception as e:
        logger.error(f"Error fetching manga from AniList: {e}")
        return await get_stale_response(cache_key) or {}


async def search_anilist_manga(client: httpx.AsyncClient, query_name: str, per_page: int = 12) -> list:
    """Manga search for the landing search. Not cached, and [] on failure."""
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
        return [media_card(m) for m in media if m.get("id")]
    except Exception as e:
        logger.error(f"Error searching manga on AniList: {e}")
        return []


async def fetch_trending_manga(client: httpx.AsyncClient, limit: int = 12) -> dict:
    """``{items, stale}`` for the landing page's trending row; ``stale`` marks
    the last known good copy served during an AniList outage."""
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
            result = [media_card(m) for m in media if m.get("id")]
    except Exception as e:
        logger.error(f"Error fetching trending manga from AniList: {e}")

    if result:
        await set_cached_response_shadowed(cache_key, result, ttl_seconds=TRENDING_CACHE_TTL)
        return {"items": result, "stale": False}

    stale = await get_stale_response(cache_key)
    if stale:
        return {"items": stale, "stale": True}
    return {"items": [], "stale": False}


# The browse hubs' sort tokens. Trending is the default, matching the trending row.
MEDIA_SORTS = {
    "trending": "TRENDING_DESC",
    "popular": "POPULARITY_DESC",
    "score": "SCORE_DESC",
    "newest": "START_DATE_DESC",
    "title": "TITLE_ROMAJI",
}
CATALOGUE_DEFAULT_SORT = "trending"


async def fetch_anilist_genres(client: httpx.AsyncClient) -> list:
    """AniList's genre vocabulary, for the browse hubs' filter chips."""
    cache_key = "anilist:genres"
    local = local_get(cache_key)
    if local is not None:
        return local
    cached = await get_cached_response(cache_key)
    if cached and "genres" in cached:
        local_set(cache_key, cached["genres"])
        return cached["genres"]
    query = "query { GenreCollection }"

    async def _stale_genres() -> list:
        stale = await get_stale_response(cache_key)
        if stale and stale.get("genres"):
            local_set(cache_key, stale["genres"])
            return stale["genres"]
        return []

    try:
        response = await anilist_post(client, query)
        if response is None or response.status_code != 200:
            return await _stale_genres()
        genres = (response.json().get("data") or {}).get("GenreCollection") or []
        if genres:
            await set_cached_response_shadowed(cache_key, {"genres": genres}, ttl_seconds=CACHE_TTL)
            local_set(cache_key, genres)
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
    """``{items, page, has_next, total}`` for one page of a browse hub, live from
    AniList: there is no local manga table, and the full anime catalogue is too
    slow to ship whole, so both hubs page through AniList."""
    sort_enum = MEDIA_SORTS.get(sort, MEDIA_SORTS[CATALOGUE_DEFAULT_SORT])
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

    # A failure is not an empty page: the shadow of this exact page comes back
    # tagged `stale`, and with no shadow `unavailable` tells the caller to 503 or
    # fall back to the local DB. Failures are never cached, so a retry self-heals.
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
        # AniList can answer 200 with the real failure in `errors`.
        if payload.get("errors"):
            msg = (payload["errors"][0] or {}).get("message", "unknown error")
            logger.warning(f"AniList {kind} browse GraphQL error: {msg}")
            return await _unavailable_or_stale()
        page_data = ((payload.get("data") or {}).get("Page") or {})
        info = page_data.get("pageInfo") or {}
        media = page_data.get("media") or []
        result = {
            "items": [media_card(m, kind) for m in media if m.get("id")],
            "page": info.get("currentPage") or page,
            "has_next": bool(info.get("hasNextPage")),
            "total": info.get("total") or 0,
        }
        if result["items"]:
            await set_cached_response_shadowed(cache_key, result, ttl_seconds=TRENDING_CACHE_TTL)
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
    return await _fetch_media_catalogue(client, "MANGA", "manga", genre, sort, page, per_page)


async def fetch_anime_catalogue(
    client: httpx.AsyncClient,
    genre: Optional[str] = None,
    sort: str = CATALOGUE_DEFAULT_SORT,
    page: int = 1,
    per_page: int = 30,
) -> Dict:
    return await _fetch_media_catalogue(client, "ANIME", "anime", genre, sort, page, per_page)
