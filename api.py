import sqlite3
import asyncio
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from contextlib import asynccontextmanager

import httpx
import json
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.requests import Request
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Import all scrapers & resolvers + metadata engine
from scrapers import ALL_SCRAPERS 
from scrapers.base_scraper import BaseAnimeScraper
from scrapers.vidking_scraper import VidkingScraper
from resolvers import ALL_RESOLVERS
from resolvers.base_resolver import BaseResolver
from metadata_engine.db_handler import MappingDatabaseEngine

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configuration
class Config:
    TMDB_API_KEY = os.getenv("TMDB_API_KEY")
    DB_NAME = "anime_mappings.db"
    CACHE_TTL_SECONDS = 86400  # 24 hours
    TRENDING_CACHE_TTL_SECONDS = 21600  # 6 hours
    MAX_CONCURRENT_REQUESTS = 10
    REQUEST_TIMEOUT = 30.0
    MAX_RETRIES = 3
    RETRY_BACKOFF_FACTOR = 1.0
    
    # CORS Origins
    ALLOWED_ORIGINS = [
        "http://localhost",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "https://dev.crimsonhaven.to",
        "https://crimsonhaven.to",
        "https://www.crimsonhaven.to"
        "https://dev.crimsonhaven.to:", # for dev channel
        "https://dev-backend.crimsonhaven.to", # for dev backend, dunno if I need this
    ]
    
    @classmethod
    def validate(cls):
        if not cls.TMDB_API_KEY:
            raise ValueError("TMDB_API_KEY environment variable is not set")

Config.validate()

# TMDB Headers
TMDB_HEADERS = {
    "Authorization": f"Bearer {Config.TMDB_API_KEY}",
    "accept": "application/json"
}

# Initialize database engine
db_engine = MappingDatabaseEngine(db_name=Config.DB_NAME)

# --- LIFESPAN MANAGEMENT ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    # Startup
    logger.info("Starting up FastAPI application...")
    
    # Initialize database
    db_engine.init_db()
    
    # Run initial sync
    try:
        await db_engine.sync_database_async()
        logger.info("Initial database sync completed")
    except Exception as e:
        logger.error(f"Initial database sync failed: {e}")
    
    # Start scheduler for periodic sync
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: asyncio.create_task(db_engine.sync_database_async()),
        trigger=IntervalTrigger(hours=24),
        id="db_sync_job",
        replace_existing=True
    )
    scheduler.start()
    logger.info("Background scheduler started")
    
    # Store scheduler in app state for cleanup
    app.state.scheduler = scheduler
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    if hasattr(app.state, 'scheduler'):
        app.state.scheduler.shutdown()
    logger.info("Shutdown complete")

# Create FastAPI app with lifespan
app = FastAPI(
    title="Anime Streaming API",
    description="API for streaming anime with multi-season support",
    version="2.0.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DATABASE HELPER FUNCTIONS ---
def get_db_connection():
    """Get database connection with row factory"""
    conn = sqlite3.connect(Config.DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def get_season_group(anilist_id: int) -> Optional[Dict]:
    """Get season group information for an AniList ID"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT group_id, season_number, tmdb_season, title
                FROM season_groups 
                WHERE anilist_id = ?
            """, (anilist_id,))
            row = cursor.fetchone()
            
            if row:
                # Get all seasons in this group
                cursor.execute("""
                    SELECT anilist_id, season_number, tmdb_season
                    FROM season_groups 
                    WHERE group_id = ?
                    ORDER BY season_number
                """, (row["group_id"],))
                all_seasons = [dict(r) for r in cursor.fetchall()]
                
                return {
                    "group_id": row["group_id"],
                    "current_season": row["season_number"],
                    "tmdb_season": row["tmdb_season"],
                    "title": row["title"],
                    "total_seasons": len(all_seasons),
                    "all_seasons": all_seasons
                }
        return None
    except Exception as e:
        logger.error(f"Error getting season group: {e}")
        return None


def get_anilist_id(tmdb_id: int, season: int = 1) -> Optional[int]:
    """Query mapped AniList ID from TMDB ID and season"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT anilist_id FROM mappings WHERE tmdb_id = ? AND tmdb_season = ?",
                (tmdb_id, season)
            )
            row = cursor.fetchone()
            return row["anilist_id"] if row else None
    except Exception as e:
        logger.error(f"Database error in get_anilist_id: {e}")
        return None

def get_tmdb_mappings(anilist_id: int) -> List[Dict[str, Any]]:
    """Get all TMDB mappings for an AniList ID"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tmdb_id, tmdb_season, title_romaji FROM mappings WHERE anilist_id = ? ORDER BY tmdb_season",
                (anilist_id,)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_tmdb_mappings: {e}")
        return []

def get_anilist_ids_for_tmdb(tmdb_id: int) -> List[int]:
    """Get all distinct AniList IDs mapped to a TMDB show across all seasons"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT anilist_id FROM mappings WHERE tmdb_id = ? ORDER BY anilist_id",
                (tmdb_id,)
            )
            return [row["anilist_id"] for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_anilist_ids_for_tmdb: {e}")
        return []

def get_all_season_mappings(anilist_id: int) -> List[int]:
    """Get all season numbers mapped to an AniList ID"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tmdb_season FROM mappings WHERE anilist_id = ? ORDER BY tmdb_season",
                (anilist_id,)
            )
            return [row["tmdb_season"] for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_all_season_mappings: {e}")
        return []

# --- CACHE HELPER FUNCTIONS ---
async def get_cached_response(cache_key: str) -> Optional[Dict]:
    """Retrieve cached response from database"""
    try:
        def _query():
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT response_json FROM api_cache WHERE cache_key = ? AND expires_at > ?",
                    (cache_key, datetime.utcnow().isoformat())
                )
                row = cursor.fetchone()
                return json.loads(row["response_json"]) if row else None
        
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query)
    except Exception as e:
        logger.error(f"Cache retrieval error for key {cache_key}: {e}")
        return None

async def set_cached_response(cache_key: str, data: Dict, ttl_seconds: int = Config.CACHE_TTL_SECONDS):
    """Save response to cache"""
    if not data:
        return
    
    try:
        expires_at = (datetime.utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
        payload = json.dumps(data)
        
        def _insert():
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO api_cache (cache_key, response_json, expires_at)
                    VALUES (?, ?, ?)
                """, (cache_key, payload, expires_at))
                conn.commit()
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _insert)
    except Exception as e:
        logger.error(f"Cache storage error for key {cache_key}: {e}")

# --- TMDB API FUNCTIONS ---
async def fetch_with_retry(client: httpx.AsyncClient, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """Fetch data from API with retry logic"""
    for attempt in range(Config.MAX_RETRIES):
        try:
            response = await client.get(url, headers=TMDB_HEADERS, params=params, timeout=Config.REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:  # Rate limit
                wait_time = Config.RETRY_BACKOFF_FACTOR * (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 1}")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.warning(f"TMDB API error: Status {response.status_code} for URL {url}")
                return None
                
        except httpx.TimeoutException:
            logger.warning(f"Timeout on attempt {attempt + 1} for {url}")
            if attempt == Config.MAX_RETRIES - 1:
                return None
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
        except Exception as e:
            logger.error(f"Request error on attempt {attempt + 1}: {e}")
            if attempt == Config.MAX_RETRIES - 1:
                return None
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
    
    return None

async def fetch_tmdb_metadata(client: httpx.AsyncClient, tmdb_id: int, season: int = 1) -> Dict:
    """Fetch metadata for a specific TMDB season"""
    cache_key = f"tmdb:meta:{tmdb_id}:s{season}"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data
    
    # Fetch from TMDB
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}"
    data = await fetch_with_retry(client, url)
    
    if not data:
        # Fallback to show-level metadata if season doesn't exist
        logger.info(f"Season {season} not found for TMDB ID {tmdb_id}, falling back to show metadata")
        show_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        show_data = await fetch_with_retry(client, show_url)
        
        if show_data:
            result = {
                "summary": show_data.get("overview"),
                "poster": f"https://image.tmdb.org/t/p/w500{show_data.get('poster_path')}" if show_data.get('poster_path') else None,
                "backdrop": f"https://image.tmdb.org/t/p/original{show_data.get('backdrop_path')}" if show_data.get('backdrop_path') else None,
                "season_name": f"Season {season}",
                "air_date": None
            }
        else:
            result = {}
    else:
        result = {
            "summary": data.get("overview"),
            "poster": f"https://image.tmdb.org/t/p/w500{data.get('poster_path')}" if data.get('poster_path') else None,
            "backdrop": f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}" if data.get('backdrop_path') else None,
            "season_name": data.get("name", f"Season {season}"),
            "air_date": data.get("air_date")
        }
    
    # Cache the result
    if result:
        await set_cached_response(cache_key, result)
    
    return result

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
            "title": media.get("title", {}).get("english") or media.get("title", {}).get("romaji"),
            "title_romaji": media.get("title", {}).get("romaji"),
            "title_english": media.get("title", {}).get("english"),
            "title_native": media.get("title", {}).get("native"),
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

async def fetch_tmdb_search_results(client: httpx.AsyncClient, query: str, limit: int = 10) -> List[Dict]:
    """Search TMDB for anime titles"""
    cache_key = f"tmdb:search:{query.lower()}"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])
    
    url = "https://api.themoviedb.org/3/search/tv"
    data = await fetch_with_retry(client, url, params={"query": query, "include_adult": "false"})
    
    if not data:
        return []
    
    results = []
    for item in data.get("results", [])[:limit]:
        tmdb_id = item.get("id")
        if tmdb_id:
            anilist_ids = get_anilist_ids_for_tmdb(tmdb_id)
            if anilist_ids:
                anilist_id = anilist_ids[0]
                # Get season metadata for proper poster
                season_meta = await fetch_tmdb_metadata(client, tmdb_id, season=1)
                poster_url = season_meta.get("poster")
                
                if not poster_url and item.get('poster_path'):
                    poster_url = f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}"
                
                results.append({
                    "title": item.get("name") or item.get("original_name"),
                    "tmdb_id": tmdb_id,
                    "anilist_id": anilist_id,
                    "anilist_ids_available": anilist_ids,
                    "poster": poster_url,
                    "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
                    "vote_average": item.get("vote_average")
                })
    
    # Cache search results for 24 hours
    await set_cached_response(cache_key, {"results": results}, ttl_seconds=Config.CACHE_TTL_SECONDS)
    return results

async def fetch_trending_anime(client: httpx.AsyncClient, limit: int = 12) -> List[Dict]:
    """Fetch trending anime from TMDB"""
    cache_key = "tmdb:trending"
    
    # Check cache
    cached_data = await get_cached_response(cache_key)
    if cached_data:
        return cached_data.get("results", [])
    
    url = "https://api.themoviedb.org/3/discover/tv"
    params = {
        "page": 1,
        "include_adult": "false",
        "language": "en-US",
        "with_genres": "16",  # Animation genre
        "with_original_language": "ja",  # Japanese originals
        "sort_by": "popularity.desc",
        "vote_count.gte": 100  # Minimum votes for quality filter
    }
    
    data = await fetch_with_retry(client, url, params=params)
    
    if not data:
        return []
    
    trending_list = []
    for item in data.get("results", [])[:limit]:
        tmdb_id = item.get("id")
        if tmdb_id:
            anilist_ids = get_anilist_ids_for_tmdb(tmdb_id)
            if anilist_ids:
                anilist_id = anilist_ids[0]
                trending_list.append({
                    "title": item.get("name") or item.get("original_name"),
                    "tmdb_id": tmdb_id,
                    "anilist_id": anilist_id,
                    "anilist_ids_available": anilist_ids,
                    "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
                    "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
                    "vote_average": item.get("vote_average")
                })
    
    # Cache trending results
    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
    return trending_list

# --- SCRAPER HELPER FUNCTIONS ---
async def run_single_scraper(scraper_class, media_ctx: Dict, episode_num: int, season_num: int) -> List[str]:
    """Run a generic scraper"""
    scraper = scraper_class()
    try:
        slug = await scraper.search_anime(media_ctx)
        if not slug:
            return []
        return await scraper.get_episode_embeds(slug, season_num=season_num, episode_num=episode_num)
    except Exception as e:
        logger.error(f"Scraper error for {scraper_class.__name__}: {e}")
        return []
    finally:
        await scraper.close()

async def run_vidking_scraper_branded(scraper, media_ctx: Dict, episode_num: int, season_num: int) -> List[str]:
    """Run VidKing scraper with branding"""
    try:
        slug = await scraper.search_anime(media_ctx)
        if not slug:
            return []
        return await scraper.get_branded_embeds(
            anime_slug=slug,
            season_num=season_num,
            episode_num=episode_num
        )
    except Exception as e:
        logger.error(f"VidKing scraper error: {e}")
        return []
    finally:
        await scraper.close()

async def resolve_streams(embed_urls: List[str]) -> List[Dict]:
    """Resolve embed URLs to direct stream URLs"""
    if not embed_urls:
        return []
    
    resolver_instances = [resolver_class() for resolver_class in ALL_RESOLVERS]
    resolved_streams = []
    
    for embed_url in embed_urls:
        # Find matching resolver
        matched_resolver = None
        for resolver in resolver_instances:
            if resolver.domain_keyword in embed_url.lower():
                matched_resolver = resolver
                break
        
        if matched_resolver:
            try:
                direct_video_url = await matched_resolver.resolve(embed_url)
                if direct_video_url:
                    # Determine stream type
                    if matched_resolver.source_name == "VidKing":
                        resolved_streams.append({
                            "source": matched_resolver.source_name,
                            "type": "iframe",
                            "url": embed_url
                        })
                    else:
                        stream_type = "hls" if "m3u8" in direct_video_url.lower() else "mp4"
                        resolved_streams.append({
                            "source": matched_resolver.source_name,
                            "type": stream_type,
                            "url": direct_video_url
                        })
                else:
                    # Fallback to raw embed
                    resolved_streams.append({
                        "source": f"{matched_resolver.source_name} (Embed)",
                        "type": "iframe",
                        "url": embed_url
                    })
            except Exception as e:
                logger.error(f"Resolver error for {matched_resolver.source_name}: {e}")
                resolved_streams.append({
                    "source": f"{matched_resolver.source_name} (Error)",
                    "type": "iframe",
                    "url": embed_url
                })
        else:
            # No resolver found, return raw embed
            resolved_streams.append({
                "source": "Direct Embed",
                "type": "iframe",
                "url": embed_url
            })
    
    return resolved_streams

# --- API ENDPOINTS ---
@app.get("/")
async def root():
    """API root endpoint"""
    return {
        "name": "Anime Streaming API",
        "version": "2.0.0",
        "status": "operational",
        "endpoints": [
            "/search/anime",
            "/trending",
            "/info/{tmdb_id}",
            "/watch/{anilist_id}/{episode_number}",
            "/seasons/{anilist_id}",
            "/debug/check_seasons/{anilist_id}"
        ]
    }

@app.get("/debug/check_seasons/{anilist_id}")
async def check_available_seasons(anilist_id: int):
    """Check what seasons are actually in your database for an anime"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tmdb_id, tmdb_season, mal_id, title_romaji FROM mappings WHERE anilist_id = ? ORDER BY tmdb_season",
                (anilist_id,)
            )
            mappings = [dict(row) for row in cursor.fetchall()]
        
        # Also count total entries for this anilist_id
        cursor.execute("SELECT COUNT(*) FROM mappings WHERE anilist_id = ?", (anilist_id,))
        total_count = cursor.fetchone()[0]
        
        return {
            "anilist_id": anilist_id,
            "total_entries_in_db": total_count,
            "seasons_found": [m["tmdb_season"] for m in mappings],
            "full_mappings": mappings,
            "note": "Each entry represents a separate TMDB season for this anime"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search/anime")
async def search_anime_by_name(query_name: str = Query(..., min_length=1, description="Anime name to search")):
    """Search for anime by name"""
    if not Config.TMDB_API_KEY:
        raise HTTPException(status_code=500, detail="TMDB API key not configured")
    
    try:
        async with httpx.AsyncClient() as client:
            results = await fetch_tmdb_search_results(client, query_name)
        
        return {
            "success": True,
            "query": query_name,
            "count": len(results),
            "suggestions": results
        }
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@app.get("/trending")
async def get_trending_anime(limit: int = Query(10, ge=1, le=50, description="Number of results to return")):
    """Get trending anime"""
    try:
        async with httpx.AsyncClient() as client:
            results = await fetch_trending_anime(client, limit)
        
        return {
            "success": True,
            "count": len(results),
            "animes": results
        }
    except Exception as e:
        logger.error(f"Trending error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trending anime")


@app.get("/seasons/{anilist_id}")
async def get_anime_seasons(anilist_id: int):
    """Get all available seasons for an anime, using season_groups table if available"""
    try:
        # First, check if this anilist_id belongs to a season group
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT group_id, season_number, tmdb_season, title
                FROM season_groups 
                WHERE anilist_id = ?
            """, (anilist_id,))
            group_row = cursor.fetchone()
        
        seasons_data = []
        group_title = None
        
        if group_row:
            # This anime is part of a multi-season group
            group_id = group_row["group_id"]
            group_title = group_row["title"]  # Might be None or "Unknown Anime"
            
            # Get all seasons in this group
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT anilist_id, season_number, tmdb_season
                    FROM season_groups 
                    WHERE group_id = ?
                    ORDER BY season_number
                """, (group_id,))
                all_seasons = cursor.fetchall()
            
            # Fetch base anime info from AniList using the first season's anilist_id
            async with httpx.AsyncClient() as client:
                first_anilist = all_seasons[0]["anilist_id"] if all_seasons else anilist_id
                anime_info = await fetch_anilist_metadata(client, first_anilist)
                # Use the fetched title if group_title is missing or generic
                if not group_title or group_title == "Unknown Anime":
                    group_title = anime_info.get("title", "Unknown Anime")
            
            # Fetch metadata for each season
            async with httpx.AsyncClient() as client:
                for season_row in all_seasons:
                    with get_db_connection() as conn:
                        cursor2 = conn.cursor()
                        cursor2.execute(
                            "SELECT tmdb_id FROM mappings WHERE anilist_id = ?",
                            (season_row["anilist_id"],)
                        )
                        mapping = cursor2.fetchone()
                    
                    if mapping:
                        tmdb_id = mapping["tmdb_id"]
                        tmdb_season = season_row["tmdb_season"]
                        season_number = season_row["season_number"]
                        
                        metadata = await fetch_tmdb_metadata(client, tmdb_id, tmdb_season)
                        
                        seasons_data.append({
                            "season_number": season_number,
                            "anilist_id": season_row["anilist_id"],
                            "tmdb_id": tmdb_id,
                            "tmdb_season": tmdb_season,
                            "name": metadata.get("season_name", f"Season {season_number}"),
                            "poster": metadata.get("poster"),
                            "summary": metadata.get("summary"),
                            "air_date": metadata.get("air_date")
                        })
            
            return {
                "success": True,
                "anilist_id": anilist_id,
                "title": group_title,  # Now guaranteed to be a real title
                "total_seasons": len(seasons_data),
                "seasons": seasons_data
            }
        
        else:
            # Fallback: No season group found, use existing logic (single season)
            # ... (keep your existing single-season logic here, but also ensure title)
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT tmdb_id, tmdb_season, title_romaji 
                       FROM mappings 
                       WHERE anilist_id = ? 
                       ORDER BY tmdb_season""",
                    (anilist_id,)
                )
                mappings = [dict(row) for row in cursor.fetchall()]
            
            if not mappings:
                raise HTTPException(status_code=404, detail="Anime not found in database")
            
            async with httpx.AsyncClient() as client:
                for idx, mapping in enumerate(mappings, start=1):
                    tmdb_id = mapping["tmdb_id"]
                    tmdb_season = mapping["tmdb_season"]
                    metadata = await fetch_tmdb_metadata(client, tmdb_id, tmdb_season)
                    
                    seasons_data.append({
                        "season_number": idx,
                        "anilist_id": anilist_id,
                        "tmdb_id": tmdb_id,
                        "tmdb_season": tmdb_season,
                        "name": metadata.get("season_name", f"Season {idx}"),
                        "poster": metadata.get("poster"),
                        "summary": metadata.get("summary"),
                        "air_date": metadata.get("air_date")
                    })
                
                # Get title from AniList
                anime_info = await fetch_anilist_metadata(client, anilist_id)
            
            return {
                "success": True,
                "anilist_id": anilist_id,
                "title": anime_info.get("title", "Unknown Anime"),
                "total_seasons": len(seasons_data),
                "seasons": seasons_data
            }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Seasons error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/info/{tmdb_id}")
async def get_anime_info(
    tmdb_id: int, 
    season: int = Query(1, ge=1, description="Season number (TMDB season)")
):
    """Get detailed information about an anime by TMDB ID"""
    try:
        # Get AniList ID from mapping
        anilist_id = get_anilist_id(tmdb_id, season)
        if not anilist_id:
            raise HTTPException(
                status_code=404, 
                detail=f"No mapping found for TMDB ID {tmdb_id} season {season}"
            )
        
        async with httpx.AsyncClient() as client:
            # Fetch metadata in parallel
            tmdb_task = fetch_tmdb_metadata(client, tmdb_id, season)
            anilist_task = fetch_anilist_metadata(client, anilist_id)
            
            tmdb_data, anilist_data = await asyncio.gather(tmdb_task, anilist_task)
        
        # Get all AniList IDs for this TMDB show (multi-season support)
        all_anilist_ids = get_anilist_ids_for_tmdb(tmdb_id)
        
        # Get available seasons for this anime
        available_seasons = get_all_season_mappings(anilist_id)
        
        merged_response = {
            "success": True,
            "tmdb_id": tmdb_id,
            "anilist_id": anilist_id,
            "all_anilist_ids": all_anilist_ids,
            "current_season": season,
            "available_seasons": available_seasons,
            **tmdb_data,
            **anilist_data
        }
        
        return merged_response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Info error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch anime information")

@app.get("/watch/{anilist_id}/{episode_number}")
async def get_streaming_links(
    anilist_id: int,
    episode_number: int,  # FastAPI automatically treats this as a path parameter
    season_part: int = Query(1, ge=1, description="Season part (1 = first mapped season, 2 = second, etc.)")
):
    """Get streaming links for a specific episode"""
    try:
        # Get all TMDB mappings for this AniList ID
        mappings = get_tmdb_mappings(anilist_id)
        
        if not mappings:
            raise HTTPException(
                status_code=404,
                detail=f"No TMDB mapping found for AniList ID {anilist_id}"
            )
        
        # Determine which mapping to use based on season_part
        if season_part > len(mappings):
            raise HTTPException(
                status_code=400,
                detail=f"Season part {season_part} not found. This anime has {len(mappings)} season(s)."
            )
        
        mapping_index = season_part - 1
        selected_mapping = mappings[mapping_index]
        tmdb_id = selected_mapping["tmdb_id"]
        tmdb_season = selected_mapping["tmdb_season"]
        
        # Fetch AniList metadata for title
        async with httpx.AsyncClient() as client:
            anilist_data = await fetch_anilist_metadata(client, anilist_id)
        
        anime_title = anilist_data.get("title")
        if not anime_title:
            raise HTTPException(status_code=404, detail="Could not resolve anime title")
        
        # Validate episode number against total episodes
        total_episodes = anilist_data.get("total_episodes")
        if total_episodes and episode_number > total_episodes:
            raise HTTPException(
                status_code=400,
                detail=f"Episode {episode_number} exceeds total episodes ({total_episodes})"
            )
        
        # Prepare media context for scrapers
        media_ctx = {
            "title": anime_title,
            "anilist_id": anilist_id,
            "tmdb_id": tmdb_id,
            "tmdb_season": tmdb_season,
            "season_part": season_part,
            **anilist_data
        }
        
        # Run scrapers in parallel
        tasks = []
        for scraper in ALL_SCRAPERS:
            if scraper.__name__ == "VidkingScraper" or "VidKing" in scraper.__class__.__name__:
                tasks.append(run_vidking_scraper_branded(
                    scraper(), media_ctx, episode_number, tmdb_season
                ))
            else:
                tasks.append(run_single_scraper(
                    scraper, media_ctx, episode_number, tmdb_season
                ))
        
        results = await asyncio.gather(*tasks)
        
        # Flatten and deduplicate embed URLs
        all_embeds = []
        for embed_list in results:
            all_embeds.extend(embed_list)
        unique_embeds = list(dict.fromkeys(all_embeds))  # Preserve order while deduping
        
        # Resolve streams
        streams = await resolve_streams(unique_embeds)
        
        return {
            "success": True,
            "anime_id": anilist_id,
            "tmdb_id": tmdb_id,
            "season": season_part,
            "tmdb_season": tmdb_season,
            "episode": episode_number,
            "title": anime_title,
            "total_episodes": total_episodes,
            "streams": streams,
            "streams_available": len(streams) > 0
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Watch error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get streaming links: {str(e)}")

# --- HEALTH CHECK ENDPOINT ---
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Check database connectivity
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM mappings")
            count = cursor.fetchone()[0]
        
        return {
            "status": "healthy",
            "database": "connected",
            "mappings_count": count,
            "scrapers_available": len(ALL_SCRAPERS),
            "resolvers_available": len(ALL_RESOLVERS)
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)}
        )

# --- ERROR HANDLERS ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "status_code": exc.status_code
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "detail": str(exc) if os.getenv("DEBUG") else None
        }
    )