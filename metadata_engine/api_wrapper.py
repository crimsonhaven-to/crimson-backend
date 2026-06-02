import sqlite3
import asyncio
import os
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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
    """Fetches posters, descriptions, and backdrops from TMDB."""
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
    response = await client.get(url)
    if response.status_code != 200:
        return {}
    data = response.json()
    return {
        "summary": data.get("overview"),
        "poster": f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}",
        "backdrop": f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}"
    }

async def fetch_anilist_metadata(client: httpx.AsyncClient, anilist_id: int) -> dict:
    """Fetches anime-specific data like episodes, status, and streaming banners."""
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
      }
    }
    """
    response = await client.post(url, json={"query": query, "variables": {"id": anilist_id}})
    if response.status_code != 200:
        return {}
    media = response.json().get("data", {}).get("Media", {})
    return {
        "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
        "total_episodes": media.get("episodes"),
        "status": media.get("status"),
        "banner": media.get("bannerImage")
    }

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

# To run this locally, install uvicorn and run: uvicorn filename:app --reload