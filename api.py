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
#TODO: Implement search and lookup logic from frontend since it crashes as of right now
'''
INFO:     127.0.0.1:54043 - "GET /info/Shadow HTTP/1.1" 422 Unprocessable Content
INFO:     127.0.0.1:54043 - "GET /search/anime?query_name=Shadow HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
'''

CRIMSON_RED = "990000" 


#TODO: In production, I shoud probably lock this down to just my onw URL
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)


load_dotenv() 

TMDB_API_KEY = os.getenv("TMDB_API_KEY") 
DB_NAME = "./metadata_engine/anime_mappings.db"
TMDB_HEADERS = {
    "Authorization": f"Bearer {TMDB_API_KEY}",
    "accept": "application/json"
}

# --- HELPER FUNCTIONS (Unchanged but included for completeness) ---

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
    
    headers = {
        "Authorization": f"Bearer {TMDB_API_KEY}",
        "accept": "application/json"
    }
    
    try:
        response = await client.get(url, headers=headers)
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
        
        # Parse the streaming episodes into a cleaner format for our frontend
        raw_episodes = media.get("streamingEpisodes", [])
        formatted_episodes = []
        
        for index, ep in enumerate(raw_episodes, start=1):
            ep_title = ep.get("title", f"Episode {index}")
            
            formatted_episodes.append({
                "episode_number": index,
                "title": ep_title,
                "thumbnail": ep.get("thumbnail")
            })

        # Fallback: If AniList has no streaming episode data, generate a dummy list based on total episodes.
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

# --- NEW HELPER FUNCTIONS FOR SEARCH & TRENDING ---

async def fetch_tmdb_search_results(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Searches TMDB by name using v4 Auth and returns a filtered list of matches."""
    print(f"[TMDB Search] Querying for anime titles containing '{query}'...")
    
    # URL clean of API keys in query parameters
    url = f"https://api.themoviedb.org/3/search/tv?query={httpx.utils.quote(query)}"
    
    try:
        # Headers parameter passes the Bearer token securely
        response = await client.get(url, headers=TMDB_HEADERS)
        if response.status_code != 200:
            print(f"[TMDB Search] Error: Status {response.status_code} - {response.text}")
            return []

        data = response.json().get("results", [])
        
        # Filter and enrich results by checking local database mapping
        filtered_results = []
        for item in data[:10]: # Limit to top 10 suggestions for performance
            tmdb_id = item.get("id")
            if tmdb_id:
                anilist_id = get_anilist_id(tmdb_id, season=1)
                
                # Only keep results that we can actually track episode data for
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
    
    # URL clean of API keys in query parameters
    url = "https://api.themoviedb.org/3/tv/popular?page=1&include_adult=false&language=en-US"
    
    try:
        # Headers parameter passes the Bearer token securely
        response = await client.get(url, headers=TMDB_HEADERS)
        if response.status_code != 200:
            print(f"[Trending] Error: Status {response.status_code} - {response.text}")
            return []

        data = response.json().get("results", [])
        trending_list = []

        for item in data[:10]: # Limit to the top 10 as requested
            tmdb_id = item.get("id")
            if tmdb_id:
                anilist_id = get_anilist_id(tmdb_id, season=1)
                
                # Only include if we have a traceable AniList ID
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


# New endpoints

@app.get("/search/anime")
async def search_anime_by_name(query_name: str):
    """
    Endpoint to search for anime by name, using TMDB and mapping results 
    to local AniList IDs.
    """
    if not query_name or not httpx.AsyncClient.__self__.headers["Authorization"]:
        raise HTTPException(status_code=400, detail="Missing API Key or Query Name.")

    async with httpx.AsyncClient() as client:
        results = await fetch_tmdb_search_results(client, query_name)
    
    return {
        "query": query_name,
        "suggestions": results if results else [{"message": "No tracked anime found matching that title."}]
    }

@app.get("/trending")
async def get_trending_anime():
    """Returns a list of the top 10 trending, trackable anime."""
    async with httpx.AsyncClient() as client:
        results = await fetch_trending_anime(client)
    
    return {
        "success": True,
        "count": len(results),
        "animes": results
    }

# old endpoints, just  (Modified and included for continuity) ---


@app.get("/info/{tmdb_id}")
async def get_anime_info(tmdb_id: int, season: int = 1):
    # ... (Existing code remains unchanged) ...
    """Queries the local SQLite database for the mapped AniList ID."""
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


async def run_single_scraper(scraper_class, media_ctx: dict, episode_num: int) -> list[str]:
    # ... (Function body remains unchanged) ...
    scraper = scraper_class()
    try:
        title_for_log = media_ctx.get("title", "Unknown Title")
        print(f"[{scraper_class.__name__}] Processing: '{title_for_log}'")
        
        slug = await scraper.search_anime(media_ctx)
        
        if not slug:
            print(f"[{scraper_class.__name__}] Anime matching data not found on this source.")
            return []
            
        print(f"[{scraper_class.__name__}] Resolved target identifier: '{slug}'. Fetching episode {episode_num}...")
        embeds = await scraper.get_episode_embeds(slug, episode_num)
        return embeds
        
    except Exception as e:
        print(f"[{scraper_class.__name__}] Error during scraping: {e}")
        return []
    finally:
        await scraper.close()


async def run_vidking_scraper_branded(
    scraper: 'VidkingScraper', 
    media_ctx: dict, 
    episode_num: int
) -> list[str]:
    # ... (Function body remains unchanged) ...
    try:
        title_for_log = media_ctx.get("title", "Unknown Title")
        print(f"[VidKingScraper] Processing: '{title_for_log}' (Branded Mode)")

        slug = await scraper.search_anime(media_ctx)
        
        if not slug:
            print("[VidKingScraper] Anime matching data not found on this source.")
            return []
            
        print(f"[VidKingScraper] Resolved target identifier: '{slug}'. Generating branded embeds for episode {episode_num}...")

        embeds = await scraper.get_branded_embeds(
            anime_slug=slug, 
            season_num=1, 
            episode_num=episode_num,
            color_code=CRIMSON_RED,
            auto_play=True
        )

        return embeds
        
    except Exception as e:
        print(f"[VidKingScraper] CRITICAL ERROR during specialized scraping: {type(e).__name__}: {e}")
        return []
    finally:
        await scraper.close()


@app.get("/watch/{anilist_id}/{episode_number}")
async def get_streaming_links(anilist_id: int, episode_number: int):
    # ... (Function body remains unchanged) ...
    """
    The orchestrator route. Resolves metadata and runs scrapers, 
    applying source-aware logic to determine the final stream type (HLS/MP4 vs iFrame).
    """
    # Metadata Fetching
    async with httpx.AsyncClient() as client:
        anilist_data = await fetch_anilist_metadata(client, anilist_id)
    
    anime_title = anilist_data.get("title")
    if not anime_title:
        raise HTTPException(status_code=404, detail="Could not resolve anime title from AniList ID.")


    # Database lookup 
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT tmdb_id FROM mappings WHERE anilist_id = ?", (anilist_id,))
    row = cursor.fetchone()
    conn.close()
    
    tmdb_id = row[0] if row else None


    # 2. Build the complete context dictionary object!
    media_ctx = {
        "title": anime_title,
        "anilist_id": anilist_id,
        "tmdb_id": tmdb_id, 
        **anilist_data
    }


    # 3. Check for available scrapers
    if not ALL_SCRAPERS:
        return {
            "anime_id": anilist_id,
            "episode": episode_number,
            "title": anime_title,
            "streams": []
        }


    # 4. Fire off all scraping tasks concurrently (FIX APPLIED HERE)
    tasks = [] 

    for scraper in ALL_SCRAPERS:
        source_name = scraper.__class__.__name__
        
        if "VidKing" in source_name:
            print(f"\n[Orchestrator] *** Specialized Path Detected: {source_name} ***", file=sys.stderr)
            tasks.append(run_vidking_scraper_branded(scraper, media_ctx, episode_number))
        else:
            print(f"\n[Orchestrator] --- Standard Path Detected: {source_name} ---", file=sys.stderr)
            tasks.append(run_single_scraper(scraper, media_ctx, episode_number))


    # Execute all tasks concurrently and await the results!
    results = await asyncio.gather(*tasks)


    # 5. Flatten the list of lists into a single flat list of unique embed links
    flattened_embeds = list(set([embed for sublist in results for embed in sublist]))


    # THE RESOLVER INTERCEPTION LAYER (Logic block remains unchanged)
    final_streams = []
    resolver_instances = [resolver_class() for resolver_class in ALL_RESOLVERS]

    for embed_url in flattened_embeds:
        matched_resolver = None
        # Use the URL itself to determine which resolver to use.
        for resolver in resolver_instances:
            if resolver.domain_keyword in embed_url.lower():
                matched_resolver = resolver
                break
        
        if matched_resolver:
            print(f"\n[Orchestrator] -> Routing resolution request for {embed_url} to {matched_resolver.source_name}.")
            direct_video_url = await matched_resolver.resolve(embed_url)
            
            # Determine stream type based on source name priority:
            if direct_video_url:
                
                # Source-Specific Stream Typing Logic (VidKing Fix)
                if matched_resolver.source_name == "VidKing":
                    stream_type = "iframe" 
                    final_streams.append({
                        "source": matched_resolver.source_name,
                        "type": stream_type, # Forcing 'iframe'
                        "url": embed_url    # Using the original validated embed link
                    })
                else:
                    # All other sources infer type based on manifest/file signatures.
                    stream_type = "hls" if "m3u8" in direct_video_url else "mp4"
                    final_streams.append({
                        "source": matched_resolver.source_name,
                        "type": stream_type, 
                        "url": direct_video_url # Use the resolved manifest/direct link
                    })

            else:
                # Fallback to iframe on resolution failure.
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
