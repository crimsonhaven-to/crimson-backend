import sqlite3
import asyncio
import os
import sys
import httpx
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Dict, Optional, List
from fastapi import Query
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
# Assuming the scraper and resolver imports are handled by their respective module structure
from scrapers import ALL_SCRAPERS 
from scrapers.base_scraper import BaseAnimeScraper
from scrapers.vidking_scraper import VidkingScraper
from resolvers import ALL_RESOLVERS
from resolvers.base_resolver import BaseResolver
from metadata_engine.db_handler import MappingDatabaseEngine 

app = FastAPI()

# import .env variables
load_dotenv()

# global vars
TMDB_API_KEY = os.getenv("TMDB_API_KEY") 
DB_NAME = "anime_mappings.db"
TMDB_HEADERS = {
    "Authorization": f"Bearer {TMDB_API_KEY}",
    "accept": "application/json"
}
 

# Database logic
db_engine = MappingDatabaseEngine(db_name="anime_mappings.db") # Set DB Engine with our desired database name
db_engine.init_db()  # Ensure DB is initialized on startup
scheduler = BackgroundScheduler()
scheduler.add_job(db_engine.run_sync, 'interval', hours=24)  # Schedule sync every 24 hours
scheduler.start()

origins = [
    "http://localhost",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://127.0.0.1"
    #TODO: Add trusted domains here for production
]

# CORS stuff
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,       # Allow specific origins
    allow_credentials=True,      # Allows cookies/auth headers to be sent with requests
    allow_methods=["*"],         # Allow all methods (GET, POST, etc.)
    allow_headers=["*"],         # Allow all headers
)


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

# Cache helper functions (we don't want to risk getting rate-limited on the TMDB API)
async def get_cached_response(cache_key: str) -> Optional[dict]:
    """Retrieves a non-expired cached JSON payload."""
    loop = asyncio.get_event_loop()
    def _query():
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT response_json FROM api_cache WHERE cache_key = ? AND expires_at > ?",
            (cache_key, datetime.utcnow().isoformat())
        )
        row = cursor.fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
        
    return await loop.run_in_executor(None, _query)


async def set_cached_response(cache_key: str, data: dict, ttl_seconds: int = 86400):
    """Saves a JSON payload to the cache with an expiration timestamp (Default: 24h)."""
    if not data:  # Avoid caching empty failed responses
        return
    
    expires_at = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
    payload = json.dumps(data)
    
    loop = asyncio.get_event_loop()
    def _insert():
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO api_cache (cache_key, response_json, expires_at)
            VALUES (?, ?, ?)
        """, (cache_key, payload, expires_at))
        conn.commit()
        conn.close()
        
    await loop.run_in_executor(None, _insert)

# we now use cache :D
async def fetch_tmdb_metadata(client: httpx.AsyncClient, tmdb_id: int, season: int = 1) -> dict:
    """Fetches posters, descriptions, and backdrops for a SPECIFIC season from TMDB."""
    # Unique cache key per season so they don't overwrite each other
    cache_key = f"tmdb:meta:{tmdb_id}:s{season}"
    
    # 1. Check SQLite Cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data

    # 2. Cache Miss -> Query TMDB Season Endpoint
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}?language=en-US"
    
    try:
        response = await client.get(url, headers=TMDB_HEADERS)
        if response.status_code != 200:
            print(f"TMDB Error: Status {response.status_code} for Season {season}")
            # Fallback: If a specific season fails, try to fetch generic show metadata
            if season != 1:
                return await fetch_tmdb_metadata(client, tmdb_id, season=1)
            return {}
            
        data = response.json()
        result = {
            # Season endpoints return 'overview' for that specific season
            "summary": data.get("overview"), 
            "poster": f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get('poster_path') else None,
            # Note: TMDB seasons don't always have distinct backdrops; fallback to show-level can be done in frontend
            "backdrop": f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}" if data.get('backdrop_path') else None
        }
        
        # 3. Save to Cache (TTL: 24 Hours)
        await set_cached_response(cache_key, result, ttl_seconds=86400)
        return result
        
    except Exception as e:
        print(f"TMDB Exception: {e}")
        return {}

# anilist metadata fetcher
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
    """Searches TMDB by name using v4 Auth and returns filtered matches with accurate season posters."""
    print(f"[TMDB Search] Querying for anime titles containing '{query}'...")
    
    url = "https://api.themoviedb.org/3/search/tv"
    
    try:
        response = await client.get(url, headers=TMDB_HEADERS, params={"query": query, "include_adult": "false"})
        if response.status_code != 200:
            print(f"[TMDB Search] Error: Status {response.status_code}")
            return []

        data = response.json().get("results", [])
        filtered_results = []
        
        for item in data[:10]: 
            tmdb_id = item.get("id")
            if tmdb_id:
                # 1. Use Season 1 as the entry point for your UI suggestions list
                anilist_id = get_anilist_id(tmdb_id, season=1)
                
                if anilist_id:
                    # 2.FIX: Call the metadata function to get the actual, cached season poster
                    season_meta = await fetch_tmdb_metadata(client, tmdb_id, season=1)
                    poster_url = season_meta.get("poster")
                    
                    # Fallback to show-level poster if season meta came back empty
                    if not poster_url and item.get('poster_path'):
                        poster_url = f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}"

                    filtered_results.append({
                        "title": item.get("name") or item.get("original_name"),
                        "tmdb_id": tmdb_id,
                        "anilist_id": anilist_id,
                        "poster": poster_url # Guaranteed to populate if TMDB has data
                    })

        return filtered_results

    except Exception as e:
        print(f"[TMDB Search] An error occurred during search: {e}")
        return []


async def fetch_trending_anime(client: httpx.AsyncClient) -> list[dict]: 
    """Fetches trending anime from TMDB, using a 6-hour cache layer."""
    cache_key = "tmdb:trending"
    
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        # Wrap it back into the list structure your original code expects
        return cached_data.get("results", [])

    url = (
        "https://api.themoviedb.org/3/discover/tv"
        "?page=1"
        "&include_adult=false"
        "&language=en-US"
        "&with_genres=16"          
        "&with_original_language=ja" 
        "&sort_by=popularity.desc"  
    )
    
    try:
        response = await client.get(url, headers=TMDB_HEADERS)
        if response.status_code != 200:
            print(f"[Trending] Error: Status {response.status_code}")
            return []

        data = response.json().get("results", []) 
        trending_list = []

        for item in data[:12]: 
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

        # Cache the payload (TTL: 6 hours / 21600 seconds)
        await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=21600)
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
    # 1. Correctly maps the (TMDB ID + Season) to the distinct AniList ID
    anilist_id = get_anilist_id(tmdb_id, season)
    if not anilist_id:
        raise HTTPException(status_code=404, detail="Anime mapping not found in local database.")

    async with httpx.AsyncClient() as client:
        # 2. Pass the season context here now! 👇
        tmdb_task = fetch_tmdb_metadata(client, tmdb_id, season=season)
        anilist_task = fetch_anilist_metadata(client, anilist_id)
        
        tmdb_data, anilist_data = await asyncio.gather(tmdb_task, anilist_task)

    merged_response = {
        "tmdb_id": tmdb_id,
        "anilist_id": anilist_id,
        "current_season": season,
        **tmdb_data,
        **anilist_data
    }

    return merged_response


@app.get("/watch/{anilist_id}/{episode_number}")
async def get_streaming_links(anilist_id: int, episode_number: int, season: int = 1):
    async with httpx.AsyncClient() as client:
        anilist_data = await fetch_anilist_metadata(client, anilist_id)
    
    anime_title = anilist_data.get("title")
    if not anime_title:
        raise HTTPException(status_code=404, detail="Could not resolve anime title from AniList ID.")

# --- CONNECT TO DB WITH BOTH PIECES OF CONTEXT ---
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Pull both tmdb_id AND the mapped tmdb_season
    cursor.execute("SELECT tmdb_id, tmdb_season FROM mappings WHERE anilist_id = ?", (anilist_id,))
    row = cursor.fetchone()
    conn.close()
    
    tmdb_id = row[0] if row else None
    mapped_tmdb_season = row[1] if row else 1 # Fallback to 1 if not found

    media_ctx = {
        "title": anime_title,
        "anilist_id": anilist_id,
        "tmdb_id": tmdb_id, 
        "tmdb_season": mapped_tmdb_season, # Now your scrapers have access to the real TMDB season!
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
            tasks.append(run_vidking_scraper_branded(scraper, media_ctx, episode_number, season))
        else:
            tasks.append(run_single_scraper(scraper, media_ctx, episode_number, season))

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
async def run_single_scraper(scraper_class, media_ctx: dict, episode_num: int, season_num: int) -> list[str]:
    scraper = scraper_class()
    try:
        slug = await scraper.search_anime(media_ctx)
        if not slug: return []
        return await scraper.get_episode_embeds(slug, episode_num)
    except Exception: return []
    finally: await scraper.close()

async def run_vidking_scraper_branded(scraper, media_ctx: dict, episode_num: int, season_num: int) -> list[str]:
    try:
        slug = await scraper.search_anime(media_ctx)
        if not slug: return []
        return await scraper.get_branded_embeds(anime_slug=slug, season_num=season_num, episode_num=episode_num, color_code=CRIMSON_RED, auto_play=True)
    except Exception: return []
    finally: await scraper.close()