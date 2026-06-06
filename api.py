import sqlite3
import asyncio
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from contextlib import asynccontextmanager

import httpx
import json
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.requests import Request
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Import all scrapers & resolvers + metadata engine
from scrapers import ALL_SCRAPERS
from resolvers import ALL_RESOLVERS
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
        "https://www.crimsonhaven.to",
        "https://dev-backend.crimsonhaven.to",
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
db_engine = MappingDatabaseEngine(db_name=Config.DB_NAME, tmdb_api_key=Config.TMDB_API_KEY)

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
    
    # Start scheduler for periodic sync. BackgroundScheduler runs jobs in a worker
    # thread with no running event loop, so the job spins up its own loop.
    def _scheduled_sync():
        try:
            asyncio.run(db_engine.sync_database_async())
        except Exception as e:
            logger.error(f"Scheduled sync failed: {e}")

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _scheduled_sync,
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

def get_anilist_id(tmdb_id: int, season_number: int) -> Optional[int]:
    """Query mapped AniList ID from TMDB ID and season"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT anilist_id FROM tmdb_seasons WHERE tmdb_id = ? AND season_number = ?",
                (tmdb_id, season_number)
            )
            row = cursor.fetchone()
            return row["anilist_id"] if row else None
    except Exception as e:
        logger.error(f"Database error in get_anilist_id: {e}")
        return None

def get_tmdb_season(anilist_id: int) -> Optional[Tuple[int, Optional[int]]]:
    """
    Reverse lookup: returns (tmdb_id, season_number) for an anilist_id.

    Falls back to tmdb_extras (specials/OVAs/movies), in which case
    season_number is None.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tmdb_id, season_number FROM tmdb_seasons WHERE anilist_id = ?",
                (anilist_id,)
            )
            row = cursor.fetchone()
            if row:
                return (row["tmdb_id"], row["season_number"])

            # Not a numbered season — maybe a special/OVA/movie.
            cursor.execute(
                "SELECT tmdb_id FROM tmdb_extras WHERE anilist_id = ? LIMIT 1",
                (anilist_id,)
            )
            row = cursor.fetchone()
            return (row["tmdb_id"], None) if row else None
    except Exception as e:
        logger.error(f"Database error in get_tmdb_season: {e}")
        return None

def get_show_seasons(tmdb_id: int) -> List[Dict]:
    """Returns all seasons with season_number, anilist_id, title_romaji, etc."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.season_number, s.anilist_id, e.title_romaji, e.title_english, e.anime_type
                FROM tmdb_seasons s
                JOIN anime_entries e ON s.anilist_id = e.anilist_id
                WHERE s.tmdb_id = ?
                ORDER BY s.season_number
            """, (tmdb_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_show_seasons: {e}")
        return []

def get_show_extras(tmdb_id: int) -> List[Dict]:
    """Returns specials/OVAs/movies tied to a show (from tmdb_extras)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT x.anilist_id, x.anime_type, e.title_romaji, e.title_english, e.start_year
                FROM tmdb_extras x
                LEFT JOIN anime_entries e ON x.anilist_id = e.anilist_id
                WHERE x.tmdb_id = ?
                ORDER BY e.start_year, x.anilist_id
            """, (tmdb_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_show_extras: {e}")
        return []

def get_show_info(tmdb_id: int) -> Dict:
    """Gets show info from tmdb_shows table."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tmdb_shows WHERE tmdb_id = ?", (tmdb_id,))
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Database error in get_show_info: {e}")
        return {}

def upsert_show_info(show: Dict) -> None:
    """Persist TMDB show details fetched on demand (lazy population of tmdb_shows)."""
    if not show.get("tmdb_id"):
        return
    try:
        def _write():
            with get_db_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO tmdb_shows
                        (tmdb_id, title, overview, poster_path, backdrop_path, first_air_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    show.get("tmdb_id"),
                    show.get("title"),
                    show.get("overview"),
                    show.get("poster_path"),
                    show.get("backdrop_path"),
                    show.get("first_air_date"),
                ))
                conn.commit()
        _write()
    except Exception as e:
        logger.error(f"Database error in upsert_show_info: {e}")

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
            seasons = get_show_seasons(tmdb_id)
            if seasons:
                # Use first season's anilist_id
                anilist_id = seasons[0]["anilist_id"]
                
                results.append({
                    "title": item.get("name") or item.get("original_name"),
                    "tmdb_id": tmdb_id,
                    "anilist_id": anilist_id,
                    "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
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
            seasons = get_show_seasons(tmdb_id)
            if seasons:
                anilist_id = seasons[0]["anilist_id"]
                trending_list.append({
                    "title": item.get("name") or item.get("original_name"),
                    "tmdb_id": tmdb_id,
                    "anilist_id": anilist_id,
                    "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else None,
                    "year": item.get("first_air_date", "")[:4] if item.get("first_air_date") else None,
                    "vote_average": item.get("vote_average")
                })
    
    # Cache trending results
    await set_cached_response(cache_key, {"results": trending_list}, ttl_seconds=Config.TRENDING_CACHE_TTL_SECONDS)
    return trending_list

# --- SCRAPER HELPER FUNCTIONS ---
async def run_single_scraper(scraper_class, tmdb_id: int, season_num: int, episode_num: int, anilist_data: Dict) -> List[str]:
    """Run one scraper through the unified search -> embeds pipeline."""
    scraper = scraper_class()
    try:
        media_ctx = {
            "tmdb_id": tmdb_id,
            "tmdb_season": season_num,
            **anilist_data
        }
        slug = await scraper.search_anime(media_ctx)
        if not slug:
            return []
        return await scraper.get_episode_embeds(slug, episode_num, season_num)
    except Exception as e:
        logger.error(f"Scraper error for {scraper_class.__name__}: {e}")
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
            "/show/{tmdb_id}",
            "/season/{tmdb_id}/{season_number}",
            "/watch/{tmdb_id}/{season_number}/{episode_number}",
            "/anilist/{anilist_id}"
        ]
    }

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

@app.get("/show/{tmdb_id}")
async def get_show_details(tmdb_id: int):
    """Returns show info + list of all seasons"""
    show_info = get_show_info(tmdb_id)
    if not show_info:
        # Not cached yet — fetch from TMDB and persist (lazy population of tmdb_shows).
        async with httpx.AsyncClient() as client:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
            show_data = await fetch_with_retry(client, url)
            if not show_data:
                raise HTTPException(status_code=404, detail="Show not found")
            show_info = {
                "tmdb_id": tmdb_id,
                "title": show_data.get("name"),
                "overview": show_data.get("overview"),
                "poster_path": show_data.get("poster_path"),
                "backdrop_path": show_data.get("backdrop_path"),
                "first_air_date": show_data.get("first_air_date")
            }
            upsert_show_info(show_info)

    seasons = get_show_seasons(tmdb_id)

    # Enrich seasons with TMDB metadata (poster, air_date)
    async with httpx.AsyncClient() as client:
        enriched_seasons = []
        for s in seasons:
            meta = await fetch_tmdb_metadata(client, tmdb_id, s["season_number"])
            enriched_seasons.append({
                **s,
                "name": meta.get("season_name"),
                "poster": meta.get("poster"),
                "air_date": meta.get("air_date")
            })

    return {
        "success": True,
        "show": show_info,
        "seasons": enriched_seasons,
        "extras": get_show_extras(tmdb_id)
    }

@app.get("/season/{tmdb_id}/{season_number}")
async def get_season_details(tmdb_id: int, season_number: int):
    """Returns combined TMDB season metadata + AniList metadata for that season."""
    anilist_id = get_anilist_id(tmdb_id, season_number)
    if not anilist_id:
        raise HTTPException(status_code=404, detail=f"No mapping for TMDB ID {tmdb_id} season {season_number}")
    
    async with httpx.AsyncClient() as client:
        tmdb_meta = await fetch_tmdb_metadata(client, tmdb_id, season_number)
        anilist_meta = await fetch_anilist_metadata(client, anilist_id)
    
    return {
        "success": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "anilist_id": anilist_id,
        "tmdb_metadata": tmdb_meta,
        "anilist_metadata": anilist_meta
    }

async def build_watch_response(tmdb_id: int, season_number: int, episode_number: int, anilist_id: int) -> Dict:
    """Run every scraper for an episode, resolve the embeds, and shape the response."""
    async with httpx.AsyncClient() as client:
        anilist_data = await fetch_anilist_metadata(client, anilist_id)

    if not anilist_data:
        raise HTTPException(status_code=404, detail="AniList metadata not found")

    tasks = [
        run_single_scraper(scraper_class, tmdb_id, season_number, episode_number, anilist_data)
        for scraper_class in ALL_SCRAPERS
    ]
    results = await asyncio.gather(*tasks)

    all_embeds = []
    for embed_list in results:
        all_embeds.extend(embed_list)
    unique_embeds = list(dict.fromkeys(all_embeds))

    streams = await resolve_streams(unique_embeds)

    return {
        "success": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "episode_number": episode_number,
        "anilist_id": anilist_id,
        "title": anilist_data.get("title"),
        "streams": streams
    }

@app.get("/watch/{tmdb_id}/{season_number}/{episode_number}")
async def get_watch_links(tmdb_id: int, season_number: int, episode_number: int):
    """Get streaming links using the new schema lookup."""
    anilist_id = get_anilist_id(tmdb_id, season_number)
    if not anilist_id:
        raise HTTPException(status_code=404, detail=f"Mapping not found for TMDB {tmdb_id} Season {season_number}")

    return await build_watch_response(tmdb_id, season_number, episode_number, anilist_id)


@app.get("/anilist/{anilist_id}")
async def get_anilist_mapping(anilist_id: int):
    """Returns { tmdb_id, season_number } for an anilist_id."""
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")
    
    return {
        "success": True,
        "anilist_id": anilist_id,
        "tmdb_id": mapping[0],
        "season_number": mapping[1]
    }

# --- DEPRECATED ENDPOINTS (REDIRECTS) ---
@app.get("/info/{tmdb_id}")
async def deprecated_info(tmdb_id: int, season: int = Query(1)):
    """Redirect to new /season endpoint."""
    return RedirectResponse(url=f"/season/{tmdb_id}/{season}", status_code=301)

@app.get("/watch/{anilist_id}/{episode_number}")
async def deprecated_watch(anilist_id: int, episode_number: int, season_part: int = Query(1)):
    """
    Watch by anilist_id. TV seasons redirect to the canonical /watch route;
    extras (specials/OVAs/movies) have no TMDB season number, so they are served
    directly here.
    """
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")

    tmdb_id, season_number = mapping
    if season_number is not None:
        return RedirectResponse(url=f"/watch/{tmdb_id}/{season_number}/{episode_number}", status_code=301)

    # Extra (special/OVA/movie): no numbered season — serve directly (season 1 for URL builders).
    return await build_watch_response(tmdb_id, 1, episode_number, anilist_id)

@app.get("/seasons/{anilist_id}")
async def get_seasons_compat(anilist_id: int):
    """Update to use the new schema."""
    mapping = get_tmdb_season(anilist_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="AniList ID not mapped")
    
    tmdb_id = mapping[0]
    return await get_show_details(tmdb_id)

# --- HEALTH CHECK ENDPOINT ---
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM anime_entries")
            count = cursor.fetchone()[0]
        
        return {
            "status": "healthy",
            "database": "connected",
            "entries_count": count,
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