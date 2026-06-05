import asyncio
import sqlite3
import httpx
import re
import os
import time
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict, deque
from datetime import datetime

class MappingDatabaseEngine:
    MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
    ANILIST_API_URL = "https://graphql.anilist.co"

    def __init__(self, db_name: str = "anime_mappings.db", tmdb_api_key: Optional[str] = None):
        self.db_name = db_name
        self.tmdb_api_key = tmdb_api_key or os.getenv("TMDB_API_KEY")

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Attempts to convert a value to an integer, returning None on failure."""
        if value is None:
            return None
        try:
            if isinstance(value, str) and '.' in value:
                value = value.split('.')[0]
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None

    def init_db(self):
        """Initializes the SQLite database with the new schema."""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        # Schema 1: Cache meta-information (unchanged)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # New Schema: anime_entries
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS anime_entries (
                anilist_id INTEGER PRIMARY KEY,
                mal_id INTEGER,
                title_romaji TEXT,
                title_english TEXT,
                anime_type TEXT,
                last_synced TIMESTAMP
            )
        ''')

        # New Schema: tmdb_shows
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tmdb_shows (
                tmdb_id INTEGER PRIMARY KEY,
                title TEXT,
                overview TEXT,
                poster_path TEXT,
                backdrop_path TEXT,
                first_air_date TEXT
            )
        ''')

        # New Schema: tmdb_seasons
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tmdb_seasons (
                tmdb_id INTEGER,
                season_number INTEGER,
                anilist_id INTEGER NOT NULL,
                PRIMARY KEY (tmdb_id, season_number),
                FOREIGN KEY (anilist_id) REFERENCES anime_entries(anilist_id)
            )
        ''')

        # New Schema: show_groups
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS show_groups (
                group_id INTEGER PRIMARY KEY,
                title TEXT,
                poster TEXT
            )
        ''')

        # New Schema: group_members
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_members (
                group_id INTEGER,
                tmdb_id INTEGER,
                season_offset INTEGER,
                PRIMARY KEY (group_id, tmdb_id)
            )
        ''')

        # Schema 4: API Cache (unchanged)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT,
                expires_at TIMESTAMP
            )
        ''')
        
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_anilist_id_entries ON anime_entries(anilist_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tmdb_seasons_lookup ON tmdb_seasons(tmdb_id, season_number)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tmdb_seasons_anilist ON tmdb_seasons(anilist_id)')
        
        conn.commit()
        
        # Run migration if old tables exist
        self.migrate_old_database(conn)
        
        conn.close()
        print(f"[DB Engine] Database schema initialized at '{self.db_name}'.")

    def migrate_old_database(self, conn: sqlite3.Connection):
        """Migrates data from old mappings and season_groups tables to the new schema."""
        cursor = conn.cursor()
        
        # Check if old tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mappings'")
        if not cursor.fetchone():
            return

        print("[DB Engine] Migrating legacy data to new schema...")
        
        try:
            # Migrate mappings to anime_entries and tmdb_seasons
            cursor.execute("SELECT tmdb_id, tmdb_season, anilist_id, mal_id, title_romaji, title_english, anime_type FROM mappings")
            old_mappings = cursor.fetchall()
            
            for row in old_mappings:
                tmdb_id, tmdb_season, anilist_id, mal_id, title_romaji, title_english, anime_type = row
                
                # Insert into anime_entries
                cursor.execute('''
                    INSERT OR IGNORE INTO anime_entries (anilist_id, mal_id, title_romaji, title_english, anime_type, last_synced)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (anilist_id, mal_id, title_romaji, title_english, anime_type, datetime.utcnow().isoformat()))
                
                # Insert into tmdb_seasons
                if tmdb_id and tmdb_season:
                    cursor.execute('''
                        INSERT OR IGNORE INTO tmdb_seasons (tmdb_id, season_number, anilist_id)
                        VALUES (?, ?, ?)
                    ''', (tmdb_id, tmdb_season, anilist_id))
            
            # Migrate season_groups to show_groups (best effort)
            cursor.execute("SELECT DISTINCT group_id, title FROM season_groups")
            old_groups = cursor.fetchall()
            for group_id, title in old_groups:
                cursor.execute("INSERT OR IGNORE INTO show_groups (group_id, title) VALUES (?, ?)", (group_id, title))
                
                # Link members
                cursor.execute("SELECT anilist_id, season_number FROM season_groups WHERE group_id = ?", (group_id,))
                members = cursor.fetchall()
                for aid, snum in members:
                    # Find tmdb_id for this anilist_id
                    cursor.execute("SELECT tmdb_id FROM tmdb_seasons WHERE anilist_id = ? LIMIT 1", (aid,))
                    trow = cursor.fetchone()
                    if trow and trow[0]:
                        cursor.execute('''
                            INSERT OR IGNORE INTO group_members (group_id, tmdb_id, season_offset)
                            VALUES (?, ?, ?)
                        ''', (group_id, trow[0], (snum - 1) * 10)) # Arbitrary offset

            # Drop old tables
            cursor.execute("DROP TABLE mappings")
            cursor.execute("DROP TABLE season_groups")
            conn.commit()
            print("[DB Engine] Legacy data migration successful.")
        except Exception as e:
            print(f"[DB Engine] Migration failed: {e}")
            conn.rollback()

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

        try:
            response = await client.head(self.MAPPING_URL, follow_redirects=True)
            new_etag = response.headers.get("ETag")
        except Exception as e:
            print(f"[DB Engine] Error checking for updates: {e}")
            return None

        if current_etag == new_etag and current_etag is not None:
            return None 

        return new_etag

    async def fetch_with_retry(self, client: httpx.AsyncClient, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Fetch data from API with retry logic."""
        headers = {"Authorization": f"Bearer {self.tmdb_api_key}", "accept": "application/json"} if self.tmdb_api_key else {}
        for attempt in range(3):
            try:
                response = await client.get(url, headers=headers, params=params, timeout=15.0)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                else:
                    return None
            except Exception:
                await asyncio.sleep(1)
        return None

    async def _fetch_anilist_metadata_bulk(self, anilist_ids: List[int]) -> Dict[int, Dict]:
        """Fetches metadata from AniList in chunks."""
        results = {}
        chunk_size = 50
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(0, len(anilist_ids), chunk_size):
                chunk = anilist_ids[i:i + chunk_size]
                query_parts = []
                for idx, aid in enumerate(chunk):
                    query_parts.append(f'anime_{idx}: Media(id: {aid}, type: ANIME) {{ id title {{ romaji english }} type startDate {{ year month day }} malId }}')
                
                query = "query { " + " ".join(query_parts) + " }"
                
                try:
                    response = await client.post(self.ANILIST_API_URL, json={'query': query})
                    if response.status_code == 200:
                        data = response.json().get('data', {})
                        for key, media in data.items():
                            if media:
                                results[media['id']] = media
                    elif response.status_code == 429:
                        await asyncio.sleep(60)
                        i -= chunk_size
                        continue
                except Exception as e:
                    print(f"[AniList] Error fetching chunk: {e}")
                
                await asyncio.sleep(0.5)
                
        return results

    async def sync_database_async(self):
        """Rewritten sync routine using the new architecture."""
        self.init_db() 
        
        async with httpx.AsyncClient(timeout=45.0) as client:
            print("\n--- Starting New Sync Process ---")
            new_etag = await self._check_needs_update(client)
            
            if not new_etag:
                print("[DB Engine] Local database mappings are up-to-date.")
                return

            print("[DB Engine] Downloading mapping data...")
            try:
                response = await client.get(self.MAPPING_URL, follow_redirects=True)
                response.raise_for_status() 
                anime_data: List[Dict[str, Any]] = response.json() 
            except Exception as e:
                print(f"[DB Engine] Error fetching data: {e}")
                return

        # 1. Parse Fribb JSON
        tmdb_to_seasons = defaultdict(list)
        all_anilist_ids = set()
        
        for item in anime_data:
            anilist_id = self._safe_int(item.get("anilist_id"))
            if not anilist_id: continue
            
            tmdb_id_raw = item.get("themoviedb_id")
            tmdb_id = self._safe_int(tmdb_id_raw.get("tv")) if isinstance(tmdb_id_raw, dict) else self._safe_int(tmdb_id_raw)
            if not tmdb_id: continue

            tmdb_season = 1
            season_raw = item.get("season")
            if isinstance(season_raw, dict):
                t_val = self._safe_int(season_raw.get("tmdb"))
                if t_val is not None: tmdb_season = t_val
                elif self._safe_int(season_raw.get("tvdb")) is not None:
                    tmdb_season = self._safe_int(season_raw.get("tvdb"))

            tmdb_to_seasons[tmdb_id].append({
                "anilist_id": anilist_id,
                "season_number": tmdb_season,
                "mal_id": self._safe_int(item.get("mal_id")),
                "type": item.get("type", "TV")
            })
            all_anilist_ids.add(anilist_id)

        # 2. Bulk fetch AniList metadata
        print(f"[DB Engine] Fetching metadata for {len(all_anilist_ids)} AniList IDs...")
        al_metadata = await self._fetch_anilist_metadata_bulk(list(all_anilist_ids))

        # 3. Process and Insert
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        try:
            # Clear old entries to ensure fresh sync but keep schemas
            cursor.execute("DELETE FROM anime_entries")
            cursor.execute("DELETE FROM tmdb_seasons")
            cursor.execute("DELETE FROM tmdb_shows")

            async with httpx.AsyncClient() as client:
                for tmdb_id, seasons in tmdb_to_seasons.items():
                    # Fetch TMDB Show info
                    tmdb_show_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
                    show_data = await self.fetch_with_retry(client, tmdb_show_url)
                    
                    if show_data:
                        cursor.execute('''
                            INSERT OR REPLACE INTO tmdb_shows (tmdb_id, title, overview, poster_path, backdrop_path, first_air_date)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            tmdb_id,
                            show_data.get("name"),
                            show_data.get("overview"),
                            show_data.get("poster_path"),
                            show_data.get("backdrop_path"),
                            show_data.get("first_air_date")
                        ))

                    for s in seasons:
                        aid = s["anilist_id"]
                        meta = al_metadata.get(aid, {})
                        
                        # Skip if no title
                        if not meta.get("title", {}).get("romaji"): continue

                        # Insert into anime_entries
                        cursor.execute('''
                            INSERT OR REPLACE INTO anime_entries (anilist_id, mal_id, title_romaji, title_english, anime_type, last_synced)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            aid,
                            meta.get("malId") or s["mal_id"],
                            meta.get("title", {}).get("romaji"),
                            meta.get("title", {}).get("english"),
                            meta.get("type") or s["type"],
                            datetime.utcnow().isoformat()
                        ))

                        # Insert into tmdb_seasons
                        cursor.execute('''
                            INSERT OR REPLACE INTO tmdb_seasons (tmdb_id, season_number, anilist_id)
                            VALUES (?, ?, ?)
                        ''', (tmdb_id, s["season_number"], aid))

            cursor.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('etag', ?)", (new_etag,))
            conn.commit()
            print(f"🎉 [DB Engine] Sync complete! Shows: {len(tmdb_to_seasons)}, Seasons: {len(all_anilist_ids)}")
            
        except Exception as e:
            print(f"[DB Engine] Error during sync: {e}")
            conn.rollback()
        finally:
            conn.close()

    def run_sync(self):
        """Wrapper for running sync."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if loop.is_running():
            asyncio.create_task(self.sync_database_async())
        else:
            loop.run_until_complete(self.sync_database_async())

if __name__ == "__main__":
    engine = MappingDatabaseEngine()
    engine.run_sync()
