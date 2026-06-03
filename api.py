import sqlite3
import asyncio
import os
import sys
import httpx
from dotenv import load_dotenv
from typing import Dict, Optional, List
from fastapi import Query
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
# Assuming the scraper and resolver imports are handled by their respective module structure
from scrapers import ALL_SCRAPERS 
from scrapers.base_scraper import BaseAnimeScraper
from scrapers.vidking_scraper import VidkingScraper
from resolvers import ALL_RESOLVERS
from resolvers.base_resolver import BaseResolver

app = FastAPI()

CRIMSON_RED = "990000" 

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv() 

TMDB_API_KEY = os.getenv("TMDB_API_KEY") 
DB_NAME = "anime_mappings.db"
TMDB_HEADERS = {
    "Authorization": f"Bearer {TMDB_API_KEY}",
    "accept": "application/json"
}

# --- HELPER FUNCTIONS ---

def get_anilist_id(tmdb_id: int, season: int = 1) -> int | None:
    """Queries the local SQLite database for the mapped AniList ID."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT anilist_id FROM mappings WHERE tmdb_id = ? AND tmdb_season = ?", 
        (tmdb_id, season)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


async def fetch_tmdb_metadata(client: httpx.AsyncClient, tmdb_id: int) -> dict:
    """Fetches posters, descriptions, and backdrops from TMDB using a v4 Read Access Token."""
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?language=en-US"
    
    try:
        response = await client.get(url, headers=TMDB_HEADERS)
        if response.status_code != 200:
            print(f"TMDB Error: Status {response.status_code}")
            return {}
            
        data = response.json()
        return {
            "summary": data.get("overview"),
            "poster": f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get('poster_path') else None,
            "backdrop": f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}" if data.get('backdrop_path') else None
        }
    except Exception as e:
        print(f"TMDB Exception: {e}")
        return {}


async def fetch_anilist_metadata(client: httpx.AsyncClient, anilist_id: int) -> dict:
    """Fetches anime-specific data, including a detailed episode list and titles."""
    url = "https://graphql.anilist.co"
    
    query = """
    query ($id: Int) {
      Media (id: $id, type: ANIME) {
        status
        episodes
        bannerImage
        title {
          romaji
          english
        }
        streamingEpisodes {
          title
          thumbnail
          url
        }
      }
    }
    """
    
    try:
        response = await client.post(url, json={"query": query, "variables": {"id": anilist_id}})
        if response.status_code != 200:
            return {}
        
        media = response.json().get("data", {}).get("Media", {})
        raw_episodes = media.get("streamingEpisodes", [])
        formatted_episodes = []
        
        for index, ep in enumerate(raw_episodes, start=1):
            ep_title = ep.get("title", f"Episode {index}")
            formatted_episodes.append({
                "episode_number": index,
                "title": ep_title,
                "thumbnail": ep.get("thumbnail")
            })

        if not formatted_episodes and media.get("episodes"):
            for i in range(1, media.get("episodes") + 1):
                formatted_episodes.append({
                    "episode_number": i,
                    "title": f"Episode {i}",
                    "thumbnail": None
                })

        return {
            "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
            "total_episodes": media.get("episodes"),
            "status": media.get("status"),
            "banner": media.get("bannerImage"),
            "episodes_list": formatted_episodes 
        }
        
    except Exception as e:
        print(f"Error fetching from AniList: {e}")
        return {}


async def fetch_tmdb_search_results(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Searches TMDB by name using v4 Auth and returns a filtered list of matches."""
    print(f"[TMDB Search] Querying for anime titles containing '{query}'...")
    
    url = "https://api.themoviedb.org/3/search/tv"
    
    try:
        # FIX: Combined query parameters AND headers into a single, secure request
        response = await client.get(url, headers=TMDB_HEADERS, params={"query": query, "include_adult": "false"})
        if response.status_code != 200:
            print(f"[TMDB Search] Error: Status {response.status_code} - {response.text}")
            return []

        data = response.json().get("results", [])
        filtered_results = []
        
        for item in data[:10]: 
            tmdb_id = item.get("id")
            if tmdb_id:
                anilist_id = get_anilist_id(tmdb_id, season=1)
                
                if anilist_id:
                    filtered_results.append({
                        "title": item.get("name") or item.get("original_name"),
                        "tmdb_id": tmdb_id,
                        "anilist_id": anilist_id,
                        "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None
                    })

        return filtered_results

    except Exception as e:
        print(f"[TMDB Search] An error occurred during search: {e}")
        return []


async def fetch_trending_anime(client: httpx.AsyncClient) -> list[dict]:
    """Fetches the top 10 trending anime using v4 Auth and maps them to AniList IDs."""
    print("[Orchestrator] Fetching globally trending anime data from TMDB...")
    
    url = "https://api.themoviedb.org/3/tv/popular?page=1&include_adult=false&language=en-US"
    
    try:
        response = await client.get(url, headers=TMDB_HEADERS)
        if response.status_code != 200:
            print(f"[Trending] Error: Status {response.status_code} - {response.text}")
            return []

        data = response.json().get("results", [])
        trending_list = []

        for item in data[:10]: 
            tmdb_id = item.get("id")
            if tmdb_id:
                anilist_id = get_anilist_id(tmdb_id, season=1)
                
                if anilist_id:
                    trending_list.append({
                        "title": item.get("name") or item.get("original_name"),
                        "tmdb_id": tmdb_id,
                        "anilist_id": anilist_id,
                        "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None
                    })

        return trending_list

    except Exception as e:
        print(f"[Trending] An error occurred during fetching: {e}")
        return []


# --- ENDPOINTS ---

@app.get("/search/anime")
async def search_anime_by_name(query_name: str):
    if not query_name or not TMDB_HEADERS.get("Authorization"):
        raise HTTPException(status_code=400, detail="Missing API Key or Query Name.")

    async with httpx.AsyncClient() as client:
        results = await fetch_tmdb_search_results(client, query_name)
    
    if results is None:
        raise HTTPException(status_code=500, detail="Internal search failure.")
        
    return {
        "query": query_name,
        "suggestions": results
    }


@app.get("/trending")
async def get_trending_anime():
    async with httpx.AsyncClient() as client:
        results = await fetch_trending_anime(client)
    
    return {
        "success": True,
        "count": len(results),
        "animes": results
    }


@app.get("/info/{tmdb_id}")
async def get_anime_info(tmdb_id: int, season: int = 1):
    anilist_id = get_anilist_id(tmdb_id, season)
    if not anilist_id:
        raise HTTPException(status_code=404, detail="Anime mapping not found in local database.")

    async with httpx.AsyncClient() as client:
        tmdb_task = fetch_tmdb_metadata(client, tmdb_id)
        anilist_task = fetch_anilist_metadata(client, anilist_id)
        
        tmdb_data, anilist_data = await asyncio.gather(tmdb_task, anilist_task)

    merged_response = {
        "tmdb_id": tmdb_id,
        "anilist_id": anilist_id,
        **tmdb_data,
        **anilist_data
    }

    return merged_response


@app.get("/watch/{anilist_id}/{episode_number}")
async def get_streaming_links(anilist_id: int, episode_number: int):
    async with httpx.AsyncClient() as client:
        anilist_data = await fetch_anilist_metadata(client, anilist_id)
    
    anime_title = anilist_data.get("title")
    if not anime_title:
        raise HTTPException(status_code=404, detail="Could not resolve anime title from AniList ID.")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT tmdb_id FROM mappings WHERE anilist_id = ?", (anilist_id,))
    row = cursor.fetchone()
    conn.close()
    
    tmdb_id = row[0] if row else None

    media_ctx = {
        "title": anime_title,
        "anilist_id": anilist_id,
        "tmdb_id": tmdb_id, 
        **anilist_data
    }

    if not ALL_SCRAPERS:
        return {
            "anime_id": anilist_id,
            "episode": episode_number,
            "title": anime_title,
            "streams": []
        }

    tasks = [] 
    for scraper in ALL_SCRAPERS:
        source_name = scraper.__class__.__name__
        if "VidKing" in source_name:
            tasks.append(run_vidking_scraper_branded(scraper, media_ctx, episode_number))
        else:
            tasks.append(run_single_scraper(scraper, media_ctx, episode_number))

    results = await asyncio.gather(*tasks)
    flattened_embeds = list(set([embed for sublist in results for embed in sublist]))

    final_streams = []
    resolver_instances = [resolver_class() for resolver_class in ALL_RESOLVERS]

    for embed_url in flattened_embeds:
        matched_resolver = None
        for resolver in resolver_instances:
            if resolver.domain_keyword in embed_url.lower():
                matched_resolver = resolver
                break
        
        if matched_resolver:
            direct_video_url = await matched_resolver.resolve(embed_url)
            if direct_video_url:
                if matched_resolver.source_name == "VidKing":
                    final_streams.append({
                        "source": matched_resolver.source_name,
                        "type": "iframe",
                        "url": embed_url
                    })
                else:
                    stream_type = "hls" if "m3u8" in direct_video_url else "mp4"
                    final_streams.append({
                        "source": matched_resolver.source_name,
                        "type": stream_type, 
                        "url": direct_video_url
                    })
            else:
                final_streams.append({
                    "source": f"{matched_resolver.source_name} (Raw Embed)",
                    "type": "iframe", 
                    "url": embed_url 
                })
        else:
            final_streams.append({
                "source": "Unknown Source (Fallback)",
                "type": "iframe",
                "url": embed_url
            })

    return {
        "anime_id": anilist_id,
        "episode": episode_number,
        "title": anime_title,
        "streams": final_streams
    }

# --- UNCHANGED SCRAPER RUNNERS ---
async def run_single_scraper(scraper_class, media_ctx: dict, episode_num: int) -> list[str]:
    scraper = scraper_class()
    try:
        slug = await scraper.search_anime(media_ctx)
        if not slug: return []
        return await scraper.get_episode_embeds(slug, episode_num)
    except Exception: return []
    finally: await scraper.close()

async def run_vidking_scraper_branded(scraper, media_ctx: dict, episode_num: int) -> list[str]:
    try:
        slug = await scraper.search_anime(media_ctx)
        if not slug: return []
        return await scraper.get_branded_embeds(anime_slug=slug, season_num=1, episode_num=episode_num, color_code=CRIMSON_RED, auto_play=True)
    except Exception: return []
    finally: await scraper.close()