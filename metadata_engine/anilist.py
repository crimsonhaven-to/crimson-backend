"""
AniList metadata fetcher, lifted out of api.py.

GraphQL fetch of a title's AniList metadata (titles, synonyms, episodes, airing
info), plus the tiny ``_empty`` coroutine api.py uses to gather an optional
AniList fetch without branching.
"""

import logging
from typing import Dict

import httpx

from core.config import Config
from core.response_cache import get_cached_response, set_cached_response

logger = logging.getLogger("crimson.anilist")


async def _empty() -> Dict:
    """A coroutine that resolves to ``{}`` — lets us ``asyncio.gather`` an
    optional fetch (e.g. AniList when there's no mapping) without branching."""
    return {}


async def fetch_anilist_metadata(client: httpx.AsyncClient, anilist_id: int) -> Dict:
    """Fetch anime metadata from AniList"""
    cache_key = f"anilist:meta:{anilist_id}"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data
    
    url = "https://graphql.anilist.co"
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
        response = await client.post(
            url, 
            json={"query": query, "variables": {"id": anilist_id}},
            timeout=Config.REQUEST_TIMEOUT
        )
        
        if response.status_code != 200:
            logger.error(f"AniList API error: Status {response.status_code}")
            return {}
        
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
        
        # Cache the result
        if result:
            await set_cached_response(cache_key, result, ttl_seconds=Config.CACHE_TTL_SECONDS)
        
        return result
        
    except Exception as e:
        logger.error(f"Error fetching from AniList: {e}")
        return {}


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
        response = await client.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"id": anilist_id}},
            timeout=Config.REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            logger.error(f"AniList manga API error: Status {response.status_code}")
            return {}
        media = (response.json().get("data") or {}).get("Media") or {}
        if not media:
            return {}

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
        await set_cached_response(cache_key, result, ttl_seconds=Config.CACHE_TTL_SECONDS)
        return result
    except Exception as e:
        logger.error(f"Error fetching manga from AniList: {e}")
        return {}


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
        response = await client.post(
            "https://graphql.anilist.co",
            json={"query": graphql, "variables": {"search": query_name, "perPage": per_page}},
            timeout=Config.REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return []
        media = ((response.json().get("data") or {}).get("Page") or {}).get("media") or []
        return [_manga_item(m) for m in media if m.get("id")]
    except Exception as e:
        logger.error(f"Error searching manga on AniList: {e}")
        return []


async def fetch_trending_manga(client: httpx.AsyncClient, limit: int = 12) -> list:
    """Trending AniList MANGA for the landing page's manga row — poster-card items.
    Cached (the list is identical for every viewer within the window)."""
    cache_key = f"anilist:manga:trending:{limit}"
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

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
    try:
        response = await client.post(
            "https://graphql.anilist.co",
            json={"query": graphql, "variables": {"perPage": limit}},
            timeout=Config.REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return []
        media = ((response.json().get("data") or {}).get("Page") or {}).get("media") or []
        result = [_manga_item(m) for m in media if m.get("id")]
        if result:
            await set_cached_response(
                cache_key, result, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS
            )
        return result
    except Exception as e:
        logger.error(f"Error fetching trending manga from AniList: {e}")
        return []
