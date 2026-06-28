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
