import asyncio
import sqlite3
import httpx
import re
import time
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict, deque

class MappingDatabaseEngine:
    MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
    ANILIST_API_URL = "https://graphql.anilist.co"

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
        
        # Schema 3: Season groups
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
        
        # Migration logic
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='mappings'")
        existing = cursor.fetchone()
        if existing:
            needs_recreate = False
            if 'PRIMARY KEY (anilist_id)' in existing[0] and 'PRIMARY KEY (anilist_id, tmdb_season)' not in existing[0]:
                needs_recreate = True
            if 'title_english' not in existing[0]:
                needs_recreate = True
                
            if needs_recreate:
                print("[DB Engine] Schema out of date. Recreating tables...")
                cursor.execute("DROP TABLE IF EXISTS mappings")
                cursor.execute("DROP TABLE IF EXISTS season_groups")
                # Re-run create table statements
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

        try:
            response = await client.head(self.MAPPING_URL, follow_redirects=True)
            new_etag = response.headers.get("ETag")
        except Exception as e:
            print(f"[DB Engine] Error checking for updates: {e}")
            return None

        if current_etag == new_etag and current_etag is not None:
            return None 

        return new_etag

    async def _fetch_anilist_metadata_bulk(self, anilist_ids: List[int]) -> Dict[int, Dict]:
        """Fetches metadata from AniList in chunks to avoid rate limits."""
        results = {}
        chunk_size = 50
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(0, len(anilist_ids), chunk_size):
                chunk = anilist_ids[i:i + chunk_size]
                
                # Construct a single query with multiple aliases
                query_parts = []
                for idx, aid in enumerate(chunk):
                    query_parts.append(f'anime_{idx}: Media(id: {aid}, type: ANIME) {{ id title {{ romaji english }} startDate {{ year month day }} }}')
                
                query = "query { " + " ".join(query_parts) + " }"
                
                try:
                    response = await client.post(self.ANILIST_API_URL, json={'query': query})
                    if response.status_code == 200:
                        data = response.json().get('data', {})
                        for key, media in data.items():
                            if media:
                                results[media['id']] = media
                    elif response.status_code == 429:
                        print("[AniList] Rate limited. Waiting...")
                        await asyncio.sleep(60)
                        # Retry this chunk
                        i -= chunk_size
                        continue
                except Exception as e:
                    print(f"[AniList] Error fetching chunk: {e}")
                
                # Small delay to be nice to AniList
                await asyncio.sleep(0.5)
                
        return results

    def _extract_base_title(self, title: str) -> str:
        """Extract base title by removing season indicators"""
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
        """Main sync routine."""
        self.init_db() 
        
        async with httpx.AsyncClient(timeout=45.0) as client:
            print("\n--- Starting Sync Process ---")
            new_etag = await self._check_needs_update(client)
            
            if not new_etag:
                print("[DB Engine] Local database mappings are up-to-date.")
                return

            print("[DB Engine] Downloading mapping data...")
            try:
                response = await client.get(self.MAPPING_URL, follow_redirects=True)
                response.raise_for_status() 
                anime_data: List[Dict[str, Any]] = response.json() 
                print(f"[DB Engine] Downloaded {len(anime_data)} entries")
            except Exception as e:
                print(f"[DB Engine] Error fetching data: {e}")
                return

        # --- Graph Building & Grouping ---
        adj = defaultdict(set)
        anilist_to_info = {}
        
        print("[DB Engine] Processing entries and building relationship graph...")
        
        for item in anime_data:
            anilist_id = self._safe_int(item.get("anilist_id"))
            if not anilist_id:
                continue
            
            # IDs that identify a "Show" (can be shared across seasons)
            tmdb_id_raw = item.get("themoviedb_id")
            tmdb_id = self._safe_int(tmdb_id_raw.get("tv")) if isinstance(tmdb_id_raw, dict) else self._safe_int(tmdb_id_raw)
            
            tvdb_id = self._safe_int(item.get("tvdb_id"))
            
            # Record basic info
            tmdb_season = 1
            season_raw = item.get("season")
            if isinstance(season_raw, dict):
                tmdb_season = self._safe_int(season_raw.get("tmdb")) or 1
                
            mal_id = self._safe_int(item.get("mal_id"))
            
            # Store info
            anilist_to_info[anilist_id] = {
                "anilist_id": anilist_id,
                "tmdb_id": tmdb_id,
                "tmdb_season": tmdb_season,
                "mal_id": mal_id,
                "tvdb_id": tvdb_id,
                "type": item.get("type", "TV")
            }
            
            # Build edges for grouping
            if tmdb_id:
                tmdb_node = f"tmdb_{tmdb_id}"
                adj[anilist_id].add(tmdb_node)
                adj[tmdb_node].add(anilist_id)
            
            if tvdb_id:
                tvdb_node = f"tvdb_{tvdb_id}"
                adj[anilist_id].add(tvdb_node)
                adj[tvdb_node].add(anilist_id)

        # --- Connected Components ---
        visited = set()
        groups = []
        
        for node in list(adj.keys()):
            if node not in visited and isinstance(node, int): # Start from an AniList ID
                component = []
                queue = deque([node])
                visited.add(node)
                
                while queue:
                    curr = queue.popleft()
                    if isinstance(curr, int):
                        component.append(curr)
                    
                    for neighbor in adj[curr]:
                        if neighbor not in visited:
                            visited.add(neighbor)
                            queue.append(neighbor)
                
                if len(component) > 1:
                    groups.append(component)

        print(f"[DB Engine] Identified {len(groups)} multi-season groups.")

        # --- Fetch Metadata ---
        target_ids = list(anilist_to_info.keys())
        print(f"[DB Engine] Fetching metadata for {len(target_ids)} AniList IDs from AniList API...")
        al_metadata = await self._fetch_anilist_metadata_bulk(target_ids)
        
        # --- Prepare Data for Insertion ---
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        try:
            cursor.execute("DELETE FROM mappings")
            cursor.execute("DELETE FROM season_groups")
            
            # 1. Insert Core Mappings
            print("[DB Engine] Inserting core mappings...")
            for aid, info in anilist_to_info.items():
                meta = al_metadata.get(aid, {})
                title_romaji = meta.get("title", {}).get("romaji", "Unknown Anime")
                title_english = meta.get("title", {}).get("english") or title_romaji
                
                cursor.execute('''
                    INSERT INTO mappings 
                    (tmdb_id, tmdb_season, anilist_id, mal_id, title_romaji, title_english, anime_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    info["tmdb_id"], 
                    info["tmdb_season"], 
                    aid, 
                    info["mal_id"], 
                    title_romaji,
                    title_english,
                    info["type"]
                ))
            
            # 2. Insert Season Groups
            print("[DB Engine] Creating season groups...")
            group_id_counter = 1
            for group in groups:
                group_info = []
                for aid in group:
                    meta = al_metadata.get(aid, {})
                    sd = meta.get("startDate", {})
                    # Create a sortable date
                    year = sd.get("year") or 9999
                    month = sd.get("month") or 12
                    day = sd.get("day") or 31
                    date_val = year * 10000 + month * 100 + day
                    
                    group_info.append({
                        "anilist_id": aid,
                        "date": date_val,
                        "title": meta.get("title", {}).get("romaji", "Unknown Anime"),
                        "tmdb_season": anilist_to_info[aid]["tmdb_season"]
                    })
                
                # Sort by start date
                group_info.sort(key=lambda x: x["date"])
                
                # Base title from the first season found
                base_title = self._extract_base_title(group_info[0]["title"])
                
                for idx, entry in enumerate(group_info, start=1):
                    cursor.execute('''
                        INSERT INTO season_groups (group_id, anilist_id, season_number, tmdb_season, title)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        group_id_counter,
                        entry["anilist_id"],
                        idx,
                        entry["tmdb_season"],
                        base_title
                    ))
                group_id_counter += 1
            
            cursor.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('etag', ?)", (new_etag,))
            conn.commit()
            print(f"🎉 [DB Engine] Sync complete! Mappings: {len(anilist_to_info)}, Groups: {group_id_counter - 1}")
            
        except Exception as e:
            print(f"[DB Engine] Error during database update: {e}")
            conn.rollback()
            raise
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
