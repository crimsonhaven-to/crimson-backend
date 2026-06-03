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
# Import the base classes and the list of scrapers
from scrapers import ALL_SCRAPERS # Assuming 'scrapers' module exists
from scrapers.base_scraper import BaseAnimeScraper
from scrapers.vidking_scraper import VidkingScraper # Must be available
# import the resolvers
from resolvers import ALL_RESOLVERS # Assuming 'resolvers' module exists
from resolvers.base_resolver import BaseResolver

app = FastAPI()


CRIMSON_RED = "990000" 


# Enable CORS so the frontend framework can talk to this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)


load_dotenv() 

TMDB_API_KEY = os.getenv("TMDB_API_KEY") 
DB_NAME = "./metadata_engine/anime_mappings.db"


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
            # Clean up titles (e.g., "Episode 1 - A Dog and a Chainsaw" -> "A Dog and a Chainsaw")
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


@app.get("/info/{tmdb_id}")
async def get_anime_info(tmdb_id: int, season: int = 1):
    # (Function body remains unchanged - it is clean and correct)
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
    """Manages the lifecycle of a single scraper execution using a context dictionary."""
    # (Function body remains unchanged - it is clean and correct)
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
    """
    Specialized task executor for VidKing. Generates the embed URL using predefined branded parameters.
    This function is now explicitly trusted to produce an iFrame source type link.
    """
    try:
        title_for_log = media_ctx.get("title", "Unknown Title")
        print(f"[VidKingScraper] Processing: '{title_for_log}' (Branded Mode)")

        slug = await scraper.search_anime(media_ctx)
        
        if not slug:
            print("[VidKingScraper] Anime matching data not found on this source.")
            return []
            
        print(f"[VidKingScraper] Resolved target identifier: '{slug}'. Generating branded embeds for episode {episode_num}...")

        # Use the dedicated method to build the URL with branding parameters
        embeds = await scraper.get_branded_embeds(
            anime_slug=slug, 
            season_num=1, 
            episode_num=episode_num,
            color_code=CRIMSON_RED,  # nice colour :3
            auto_play=True        # Always autoplay for the branded experience
        )

        return embeds
        
    except Exception as e:
        print(f"[VidKingScraper] CRITICAL ERROR during specialized scraping: {type(e).__name__}: {e}")
        return []
    finally:
        await scraper.close()


@app.get("/watch/{anilist_id}/{episode_number}")
async def get_streaming_links(anilist_id: int, episode_number: int):
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


    # Database lookup (Remains unchanged and correct)
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


    # 3. Check for available scrapers (Remains unchanged)
    if not ALL_SCRAPERS:
        return {
            "anime_id": anilist_id,
            "episode": episode_number,
            "title": anime_title,
            "streams": []
        }


    # 4. Fire off all scraping tasks concurrently (CORRECTION APPLIED HERE)
    tasks = [] 

    for scraper in ALL_SCRAPERS:
        source_name = scraper.__class__.__name__
        
        if "VidKing" in source_name:
            print(f"\n[Orchestrator] *** Specialized Path Detected: {source_name} ***", file=sys.stderr)
            # FIX 1: The task is now properly prepared as an awaitable call.
            tasks.append(run_vidking_scraper_branded(scraper, media_ctx, episode_number))
        else:
            print(f"\n[Orchestrator] --- Standard Path Detected: {source_name} ---", file=sys.stderr)
            # FIX 2: The task is now properly prepared as an awaitable call.
            tasks.append(run_single_scraper(scraper, media_ctx, episode_number))


    # Execute all tasks concurrently and await the results!
    results = await asyncio.gather(*tasks)


    # 5. Flatten the list of lists into a single flat list of unique embed links
    flattened_embeds = list(set([embed for sublist in results for embed in sublist]))


    # THE RESOLVER INTERCEPTION LAYER (This logic block remains correct and is kept.)
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
                
                # Source-Specific Stream Typing Logic (The VidKing Fix)
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
                # If resolution failed, we fall back to using the raw embed link as an iFrame source.
                final_streams.append({
                    "source": f"{matched_resolver.source_name} (Raw Embed)",
                    "type": "iframe", # Always default to iframe for fallback display
                    "url": embed_url 
                })
        else:
            # Safety net for unknown domains
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