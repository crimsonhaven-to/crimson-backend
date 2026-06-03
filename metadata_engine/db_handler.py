import asyncio
import sqlite3
import httpx
from typing import Optional, List, Dict, Any, Tuple

class MappingDatabaseEngine:
    MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"

    def __init__(self, db_name: str = "anime_mappings.db"):
        self.db_name = db_name

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Attempts to convert a value to an integer, returning None on failure."""
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None

    def init_db(self):
        """Initializes the SQLite database and ensures all required schemas exist."""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        # Schema 1: Cache meta-information (ETags, etc.)
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

        # Schema 3: The API Caching layer (From our previous step)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT,
                expires_at TIMESTAMP
            )
        ''')
        
        conn.commit()
        print(f"[DB Engine] Database schema verified/initialized at '{self.db_name}'.")
        conn.close()

    async def _check_needs_update(self, client: httpx.AsyncClient) -> Optional[str]:
        """Checks GitHub headers to see if a new version exists."""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT value FROM sync_meta WHERE key = 'etag'")
            row = cursor.fetchone()
            current_etag = row[0] if row else None
        finally:
            conn.close()

        response = await client.head(self.MAPPING_URL, follow_redirects=True)
        new_etag = response.headers.get("ETag")

        if current_etag == new_etag and current_etag is not None:
            return None 

        if current_etag != new_etag:
            print(f"[DB Engine] ETag change detected (Old: {current_etag} -> New: {new_etag}).")
            return new_etag

        return None

    async def sync_database_async(self):
        """Asynchronous execution routine to fetch remote adjustments and update tables."""
        self.init_db() 
        
        async with httpx.AsyncClient(timeout=45.0) as client:
            print("\n--- Starting Sync Process ---")
            new_etag = await self._check_needs_update(client)
            
            if not new_etag:
                print("[DB Engine] Local database mappings are already up-to-date. Skipping download.")
                return

            print("[DB Engine] Downloading fresh layout configurations mapping data...")
            try:
                response = await client.get(self.MAPPING_URL, follow_redirects=True)
                response.raise_for_status() 
                anime_data: List[Dict[str, Any]] = response.json() 
            except httpx.HTTPStatusError as e:
                print(f"[DB Engine] Error fetching data (HTTP Status {e.response.status_code}): {e}")
                return
            except Exception as e:
                print(f"[DB Engine] General error during download: {e}")
                return

        # --- DB Processing & Transaction ---
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        insert_buffer: List[Tuple] = []
        
        print("[DB Engine] Parsing and preparing data buffer...")

        for item in anime_data:
            # 1. TMDB ID Extraction
            tmdb_id_raw = item.get("themoviedb_id")
            tmdb_id = None
            if isinstance(tmdb_id_raw, dict):
                tmdb_id = self._safe_int(tmdb_id_raw.get("tv"))
            elif isinstance(tmdb_id_raw, (str, int)):
                 tmdb_id = self._safe_int(tmdb_id_raw)

            # 2. Season Extraction
            tmdb_season = 1 
            season_raw = item.get("season")
            if isinstance(season_raw, dict):
                tmdb_season = self._safe_int(season_raw.get("tmdb")) or 1

            # 3. Essential ID Extraction
            anilist_id = self._safe_int(item.get("anilist_id"))
            mal_id = self._safe_int(item.get("mal_id"))
            title = str(item.get("title", "Unknown Anime")).strip()

            if tmdb_id is not None and anilist_id is not None: 
                insert_buffer.append((tmdb_id, tmdb_season, anilist_id, mal_id, title))

        try:
            cursor.executemany('''
                INSERT OR REPLACE INTO mappings (tmdb_id, tmdb_season, anilist_id, mal_id, title_romaji)
                VALUES (?, ?, ?, ?, ?)
            ''', insert_buffer)

            cursor.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('etag', ?)", (new_etag,))
            conn.commit()
            print(f"🎉 [DB Engine] Sync complete! Successfully written {len(insert_buffer)} records.")
        finally:
            conn.close()

    def run_sync(self):
        """Synchronous wrapper function ideal for standard background schedulers."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if loop.is_running():
            # If an loop is already running (like FastAPI), we create a task within it
            asyncio.create_task(self.sync_database_async())
        else:
            loop.run_until_complete(self.sync_database_async())

# Allows quick localized debugging:
if __name__ == "__main__":
    engine = MappingDatabaseEngine()
    engine.run_sync()