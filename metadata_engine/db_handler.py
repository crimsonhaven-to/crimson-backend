import asyncio
import sqlite3
import httpx
from typing import Optional
from typing import List, Dict, Any, Tuple

# URL to a reliable community mapping JSON. For now: Fribb's anime-lists mapping
MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
DB_NAME = "anime_mappings.db"

# Helper function to safely cast a value to an integer if possible
def safe_int(value: Any) -> Optional[int]:
    """Attempts to convert a value to an integer, returning None on failure."""
    if value is None:
        return None
    try:
        # Handle common cases like numbers stored as strings or in dicts
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def init_db():
    """Initializes the SQLite database and ensures the schema is correct."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Schema 1: Cache meta-information
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # Schema 2: The core mappings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mappings (
            tmdb_id INTEGER,
            tmdb_season INTEGER,
            anilist_id INTEGER,
            mal_id INTEGER,
            title_romaji TEXT,
            PRIMARY KEY (tmdb_id, tmdb_season)
        )
    ''')
    conn.commit()
    print("Database schema confirmed/initialized.")
    conn.close()


async def check_needs_update(client: httpx.AsyncClient) -> str | None:
    """Checks GitHub headers to see if a new version exists. Returns the new ETag if an update is needed, otherwise None."""
    # Using 'with' statement for proper resource handling
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT value FROM sync_meta WHERE key = 'etag'")
        row = cursor.fetchone()
        current_etag = row[0] if row else None
    finally:
        conn.close()

    # Request headers only (Head request)
    response = await client.head(MAPPING_URL, follow_redirects=True)
    new_etag = response.headers.get("ETag")

    if current_etag == new_etag and current_etag is not None:
        return None # Database is already up to date!
    
    # If we didn't find an ETag previously, but the remote server has one, update it for safety.
    if current_etag != new_etag:
        print(f"ETag change detected (Old: {current_etag} -> New: {new_etag}). Update required.")
        return new_etag

    return None


async def sync_database():
    """Main routine to initialize, check for updates, and bulk-insert mapping data."""
    init_db() # Run synchronous setup first
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        print("\n--- Starting Sync Process ---")
        new_etag = await check_needs_update(client)
        
        if not new_etag:
            print("Local database is up-to-date. Skipping download.")
            return

        print("New updates found! Downloading fresh mapping data...")
        try:
            response = await client.get(MAPPING_URL, follow_redirects=True)
            # Check for HTTP failure before attempting JSON parsing
            response.raise_for_status() 
            anime_data: List[Dict[str, Any]] = response.json() 
        except httpx.HTTPStatusError as e:
            print(f"Error fetching mapping data (HTTP Status {e.response.status_code}): {e}")
            return
        except Exception as e:
            print(f"General error during JSON parsing/download: {e}")
            return

    # --- Database Transaction Start ---
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    total_records_processed = 0
    insert_buffer: List[Tuple] = []
    
    print("Parsing and preparing data buffer...")

    for item in anime_data:
        # --- Robust Extraction Logic Start ---
        
        # 1. TMDB ID Extraction
        tmdb_id_raw = item.get("themoviedb_id")
        tmdb_id = None
        if isinstance(tmdb_id_raw, dict):
            # Prioritize 'tv' over 'movie' for the primary TV show mapping use case
            extracted_tv = tmdb_id_raw.get("tv")
            tmdb_id = safe_int(extracted_tv)
        elif isinstance(tmdb_id_raw, (str, int)):
             tmdb_id = safe_int(tmdb_id_raw)

        # 2. Season Extraction
        tmdb_season = 1 # Default fallback value
        season_raw = item.get("season")
        if isinstance(season_raw, dict):
            extracted_season = season_raw.get("tmdb")
            tmdb_season = safe_int(extracted_season) or 1


        # 3. Essential ID Extraction & Conversion
        anilist_id = safe_int(item.get("anilist_id"))
        mal_id = safe_int(item.get("mal_id"))

        # 4. Title (Used for the title_romaji column)
        title = str(item.get("title", "Unknown Anime")).strip()


        # Only append to buffer if all essential IDs were successfully parsed and exist
        if tmdb_id is not None and anilist_id is not None: 
            insert_buffer.append((tmdb_id, tmdb_season, anilist_id, mal_id, title))
            total_records_processed += 1

    # --- Bulk Insertion (The optimized write step) ---
    try:
        cursor.executemany('''
            INSERT OR REPLACE INTO mappings (tmdb_id, tmdb_season, anilist_id, mal_id, title_romaji)
            VALUES (?, ?, ?, ?, ?)
        ''', insert_buffer)

        # Update ETag metadata upon successful write
        cursor.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('etag', ?)", (new_etag,))
        conn.commit()
        print(f"\n🎉 Sync complete! Successfully inserted/updated {len(insert_buffer)} records into the database.")

    finally:
        conn.close()


# To run the script standalone:
if __name__ == "__main__":
    asyncio.run(sync_database())
