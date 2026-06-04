import asyncio
import sqlite3
import httpx
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict

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
            if isinstance(value, str) and '.' in value:
                value = value.split('.')[0]
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None

    def init_db(self):
        """Initializes the SQLite database and ensures all required schemas exist."""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        # Schema 1: Cache meta-information
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Schema 2: Core mappings table
        # Composite PK allows same AniList ID to map to multiple TMDB seasons
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS mappings (
                tmdb_id INTEGER,
                tmdb_season INTEGER,
                anilist_id INTEGER,
                mal_id INTEGER,
                title_romaji TEXT,
                title_english TEXT,
                anime_type TEXT,
                PRIMARY KEY (anilist_id, tmdb_season)
            )
        ''')
        
        # Schema 3: Season groups (links multiple AniList entries together)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS season_groups (
                group_id INTEGER,
                anilist_id INTEGER,
                season_number INTEGER,
                tmdb_season INTEGER,
                title TEXT,
                PRIMARY KEY (group_id, season_number)
            )
        ''')
        
        # Migration: check if old schema (single-column PK) exists and recreate
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='mappings'")
        existing = cursor.fetchone()
        if existing and 'PRIMARY KEY (anilist_id)' in existing[0] and 'PRIMARY KEY (anilist_id, tmdb_season)' not in existing[0]:
            cursor.execute("DROP TABLE IF EXISTS mappings")
            cursor.execute("DROP TABLE IF EXISTS season_groups")
            cursor.execute('''CREATE TABLE IF NOT EXISTS mappings (
                tmdb_id INTEGER,
                tmdb_season INTEGER,
                anilist_id INTEGER,
                mal_id INTEGER,
                title_romaji TEXT,
                title_english TEXT,
                anime_type TEXT,
                PRIMARY KEY (anilist_id, tmdb_season)
            )''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS season_groups (
                group_id INTEGER,
                anilist_id INTEGER,
                season_number INTEGER,
                tmdb_season INTEGER,
                title TEXT,
                PRIMARY KEY (group_id, season_number)
            )''')
            # Clear cached ETag to force a full re-sync after schema migration
            cursor.execute("DELETE FROM sync_meta WHERE key = 'etag'")
        
        # Schema 4: API Cache
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT,
                expires_at TIMESTAMP
            )
        ''')
        
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_anilist_id ON mappings(anilist_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tmdb_id ON mappings(tmdb_id, tmdb_season)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tmdb_lookup ON mappings(tmdb_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_group_anilist ON season_groups(anilist_id)')
        
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

    def _extract_base_title(self, title: str) -> str:
        """Extract base title by removing season indicators"""
        import re
        # Remove common season indicators
        patterns = [
            r'\s*[-\–]\s*Season\s+\d+',
            r'\s*[-\–]\s+Saison\s+\d+',
            r'\s*[-\–]\s+Part\s+\d+',
            r'\s*[-\–]\s+2nd\s+Season',
            r'\s*[-\–]\s+3rd\s+Season', 
            r'\s*[-\–]\s+4th\s+Season',
            r'\s*Season\s+\d+$',
            r'\s*\(Season\s+\d+\)$',
            r'\s*\[Season\s+\d+\]$',
        ]
        
        base_title = title
        for pattern in patterns:
            base_title = re.sub(pattern, '', base_title, flags=re.IGNORECASE)
        
        return base_title.strip()

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
                print(f"[DB Engine] Downloaded {len(anime_data)} total entries from Fribb's list")
            except Exception as e:
                print(f"[DB Engine] Error fetching data: {e}")
                return

        # --- DB Processing ---
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        insert_buffer: List[Tuple] = []
        
        # First, collect all TV entries with TMDB data
        tv_entries = []
        
        for item in anime_data:
            # Only process TV entries (ignore movies/ONAs for main mappings)
            anime_type = item.get("type", "")
            if anime_type != "TV":
                continue
            
            # Extract TMDB ID
            tmdb_id_raw = item.get("themoviedb_id")
            tmdb_id = None
            
            if isinstance(tmdb_id_raw, dict):
                tmdb_id = self._safe_int(tmdb_id_raw.get("tv"))
            elif isinstance(tmdb_id_raw, (str, int)):
                tmdb_id = self._safe_int(tmdb_id_raw)
            
            if not tmdb_id:
                continue
            
            # Extract season
            tmdb_season = 1
            season_raw = item.get("season")
            if isinstance(season_raw, dict):
                tmdb_season = self._safe_int(season_raw.get("tmdb")) or 1
            
            # Extract IDs
            anilist_id = self._safe_int(item.get("anilist_id"))
            mal_id = self._safe_int(item.get("mal_id"))
            
            if not anilist_id:
                continue
            
            # Get title
            title = item.get("title", "Unknown Anime")
            
            tv_entries.append({
                "anilist_id": anilist_id,
                "tmdb_id": tmdb_id,
                "tmdb_season": tmdb_season,
                "mal_id": mal_id,
                "title": title,
                "type": anime_type
            })
        
        print(f"[DB Engine] Found {len(tv_entries)} TV entries with TMDB data")
        
        # Group by TMDB ID and detect multi-season
        tmdb_groups = defaultdict(list)
        for entry in tv_entries:
            tmdb_groups[entry["tmdb_id"]].append(entry)
        
        # Create season groups
        season_groups = []
        group_id = 1
        
        for tmdb_id, entries in tmdb_groups.items():
            if len(entries) > 1:
                # Sort by season number
                entries.sort(key=lambda x: x["tmdb_season"])
                
                # Find the base title (remove season indicators)
                base_title = self._extract_base_title(entries[0]["title"])
                
                print(f"\n[GROUP] TMDB ID {tmdb_id}: Found {len(entries)} seasons")
                for entry in entries:
                    print(f"  - Season {entry['tmdb_season']}: AniList {entry['anilist_id']} - '{entry['title']}'")
                
                for idx, entry in enumerate(entries, start=1):
                    season_groups.append({
                        "group_id": group_id,
                        "anilist_id": entry["anilist_id"],
                        "season_number": idx,
                        "tmdb_season": entry["tmdb_season"],
                        "title": base_title,
                        "original_title": entry["title"]
                    })
                group_id += 1
        
        # Also handle special case: Eminence in Shadow (different TMDB IDs but same series)
        # This is a manual mapping for series that have different TMDB IDs per season
        manual_groups = [
            {
                "name": "The Eminence in Shadow",
                "entries": [
                    {"anilist_id": 130298, "season": 1, "tmdb_id": 119495, "tmdb_season": 1},
                    {"anilist_id": 161964, "season": 2, "tmdb_id": 119495, "tmdb_season": 2},
                ]
            },
            # Add more manual groupings here as needed
        ]
        
        for manual_group in manual_groups:
            print(f"\n[MANUAL GROUP] {manual_group['name']}")
            for entry in manual_group["entries"]:
                season_groups.append({
                    "group_id": group_id,
                    "anilist_id": entry["anilist_id"],
                    "season_number": entry["season"],
                    "tmdb_season": entry["tmdb_season"],
                    "title": manual_group["name"],
                    "original_title": manual_group["name"]
                })
            group_id += 1
        
        # Insert mappings
        print(f"\n[DB Engine] Inserting {len(tv_entries)} mappings...")
        
        try:
            # Clear existing data
            cursor.execute("DELETE FROM mappings")
            cursor.execute("DELETE FROM season_groups")
            
            # Insert mappings
            for entry in tv_entries:
                cursor.execute('''
                    INSERT OR REPLACE INTO mappings 
                    (tmdb_id, tmdb_season, anilist_id, mal_id, title_romaji, title_english, anime_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    entry["tmdb_id"], 
                    entry["tmdb_season"], 
                    entry["anilist_id"], 
                    entry["mal_id"], 
                    entry["title"],
                    entry["title"],
                    entry["type"]
                ))
            
            # Insert season groups
            for group in season_groups:
                cursor.execute('''
                    INSERT INTO season_groups (group_id, anilist_id, season_number, tmdb_season, title)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    group["group_id"],
                    group["anilist_id"],
                    group["season_number"],
                    group["tmdb_season"],
                    group["title"]
                ))
            
            cursor.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('etag', ?)", (new_etag,))
            conn.commit()
            
            print(f"🎉 [DB Engine] Sync complete!")
            print(f"  - Mappings inserted: {len(tv_entries)}")
            print(f"  - Season groups created: {group_id - 1}")
            
            # Verification
            cursor.execute("SELECT anilist_id, tmdb_season FROM mappings WHERE anilist_id IN (130298, 161964)")
            eminence = cursor.fetchall()
            print(f"\n[VERIFICATION] Eminence in Shadow entries:")
            for anilist_id, season in eminence:
                print(f"  - AniList {anilist_id} -> TMDB Season {season}")
            
            cursor.execute("SELECT * FROM season_groups WHERE title LIKE '%Eminence%'")
            groups = cursor.fetchall()
            if groups:
                print(f"\n[VERIFICATION] Season group created for Eminence in Shadow:")
                for group in groups:
                    print(f"  - Group {group[0]}: AniList {group[1]} = Season {group[2]} (TMDB Season {group[3]})")
            
        except Exception as e:
            print(f"[DB Engine] Error during insert: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def run_sync(self):
        """Synchronous wrapper function for background schedulers."""
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