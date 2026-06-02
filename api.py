import sqlite3
import asyncio
import os
import httpx
from dotenv import load_dotenv
from fastapi import Query
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
# Import the base classes and the list of scrapers
from scrapers import ALL_SCRAPERS
from scrapers.base_scraper import BaseAnimeScraper
# import the resolvers
from resolvers import ALL_RESOLVERS

app = FastAPI()

# Enable CORS so the frontend framework can talk to this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  #TODO: In production, I shouold probably lock this down to just the frontend domain
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()  # Load environment variables from .env file

TMDB_API_KEY = os.getenv("TMDB_API_KEY")  
DB_NAME = "anime_mappings.db"

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
    # Notice we removed ?api_key= from the URL entirely
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?language=en-US"
    
    # Let's pass the massive token securely inside the HTTP Headers
    headers = {
        "Authorization": f"Bearer {TMDB_API_KEY}",
        "accept": "application/json"
    }
    
    try:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            print(f"TMDB Error: Status {response.status_code}") # Temporary debug line
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
    
    # Added 'streamingEpisodes' to pull titles, thumbnails, and official site URLs.
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
            # AniList often includes the episode number in the title string
            ep_title = ep.get("title", f"Episode {index}")
            
            formatted_episodes.append({
                "episode_number": index,
                "title": ep_title,
                "thumbnail": ep.get("thumbnail")
            })

        # Fallback: If AniList has no streaming episode data (common for brand-new or obscure shows),
        # we generate a dummy list based on total episodes so the frontend still has buttons to click.
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
            "episodes_list": formatted_episodes  # <-- Beautiful clean array for the UI
        }
        
    except Exception as e:
        print(f"Error fetching from AniList: {e}")
        return {}
    


@app.get("/info/{tmdb_id}")
async def get_anime_info(tmdb_id: int, season: int = 1):
    # 1. Look up the AniList counterpart in our SQLite Sync DB
    anilist_id = get_anilist_id(tmdb_id, season)
    if not anilist_id:
        raise HTTPException(status_code=404, detail="Anime mapping not found in local database.")

    # 2. Fire off HTTP requests concurrently using async
    async with httpx.AsyncClient() as client:
        tmdb_task = fetch_tmdb_metadata(client, tmdb_id)
        anilist_task = fetch_anilist_metadata(client, anilist_id)
        
        # Both APIs are called at the exact same time
        tmdb_data, anilist_data = await asyncio.gather(tmdb_task, anilist_task)

    # 3. Merge the data dictionaries together
    merged_response = {
        "tmdb_id": tmdb_id,
        "anilist_id": anilist_id,
        **tmdb_data,
        **anilist_data
    }

    return merged_response

async def run_single_scraper(scraper_class, title: str, episode_num: int) -> list[str]:
    """Helper function to manage the lifecycle of a single scraper execution."""
    # 1. Instantiate the scraper class
    scraper = scraper_class()
    try:
        print(f"🔍 [{scraper_class.__name__}] Searching for: '{title}'")
        slug = await scraper.search_anime(title)
        
        if not slug:
            print(f"[{scraper_class.__name__}] Anime not found on this source.")
            return []
            
        print(f"🔗 [{scraper_class.__name__}] Found slug: '{slug}'. Fetching episode {episode_num}...")
        embeds = await scraper.get_episode_embeds(slug, episode_num)
        return embeds
        
    except Exception as e:
        print(f"[{scraper_class.__name__}] Error during scraping: {e}")
        return []
    finally:
        # Crucial: Always close the httpx.AsyncClient connection to prevent memory leaks
        await scraper.close()


@app.get("/watch/{anilist_id}/{episode_number}")
async def get_streaming_links(anilist_id: int, episode_number: int):
    """
    The orchestrator route. It resolves the anime title via AniList,
    fires off all scrapers concurrently, and resolves video stream URLs.
    """
    # 1. We need the romaji/english title so the scrapers know what text to search for
    async with httpx.AsyncClient() as client:
        anilist_data = await fetch_anilist_metadata(client, anilist_id)
    
    anime_title = anilist_data.get("title")
    if not anime_title:
        raise HTTPException(status_code=404, detail="Could not resolve anime title from AniList ID.")

    # 2. If we have no scrapers implemented yet, return an empty list early
    if not ALL_SCRAPERS:
        return {
            "anime_id": anilist_id,
            "episode": episode_number,
            "title": anime_title,
            "streams": []
        }

    # 3. Create a list of asynchronous tasks, one for each registered provider
    tasks = [
        run_single_scraper(scraper_class, anime_title, episode_number)
        for scraper_class in ALL_SCRAPERS
    ]
    
    # 4. Fire them ALL off simultaneously using asyncio.gather
    results = await asyncio.gather(*tasks)
    
    # 5. Flatten the list of lists into a single flat list of unique embed links
    flattened_embeds = list(set([embed for sublist in results for embed in sublist]))

    # --- NEW: THE RESOLVER INTERCEPTION LAYER ---
    final_streams = []

        # Instantiate our resolvers once before entering the loop
    resolver_instances = [resolver_class() for resolver_class in ALL_RESOLVERS]

    for embed_url in flattened_embeds:
        matched_resolver = None
        
        # Check if any registered resolver owns this link
        for resolver in resolver_instances:
            if resolver.domain_keyword in embed_url.lower():
                matched_resolver = resolver
                break
            
        # If a resolver was found, use it dynamically!
        if matched_resolver:
            print(f"🎯 Dynamically routing to {matched_resolver.source_name}: {embed_url}")
            direct_video_url = await matched_resolver.resolve(embed_url)
            
            if direct_video_url:
                final_streams.append({
                    "source": matched_resolver.source_name,
                    "type": "hls" if "m3u8" in direct_video_url else "mp4",
                    "url": direct_video_url
                })
            else:
                final_streams.append({
                    "source": f"{matched_resolver.source_name} (Raw Embed)",
                    "type": "iframe",
                    "url": embed_url
                })
        else:
            # Fallback for platforms without a resolver yet
            final_streams.append({
                "source": "External Provider",
                "type": "iframe",
                "url": embed_url
            })

    return {
        "anime_id": anilist_id,
        "episode": episode_number,
        "title": anime_title,
        "streams": final_streams
    }

# To run this locally, install uvicorn and run: uvicorn filename:app --reload