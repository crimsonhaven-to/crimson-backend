import asyncio
import sqlite3
import httpx

# URL to a reliable community mapping JSON. For now: Fribb's anime-lists mapping
MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
DB_NAME = "anime_mappings.db"

def init_db():
    """Initializes the SQLite database and ensures the schema is correct."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # First, we create a table tracking both meta-information (cache) and the mappings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # Composite primary key ensures one unique slot per TMDB ID + Season combo
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
    conn.close()

async def check_needs_update(client: httpx.AsyncClient) -> str | None:
    """Checks GitHub headers to see if a new version exists.
    Returns the new ETag if an update is needed, otherwise None."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM sync_meta WHERE key = 'etag'")
    row = cursor.fetchone()
    current_etag = row[0] if row else None
    conn.close()

    # Request headers only, don't download the massive JSON yet
    response = await client.head(MAPPING_URL)
    new_etag = response.headers.get("ETag")

    if current_etag == new_etag:
        return None # Database is already perfectly up to date!
    
    return new_etag

async def sync_database():
    init_db()
    
    async with httpx.AsyncClient() as client:
        print("Checking GitHub for mapping updates...")
        new_etag = await check_needs_update(client)
        
        if not new_etag:
            print("Local database is already up-to-date. Skipping download.")
            return

        print("New updates found! Downloading fresh mapping data...")
        response = await client.get(MAPPING_URL)
        anime_data = response.json() # This will be an array of anime objects

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        print("Populating SQLite database...")
        
        # Prepare data for bulk insertion (much faster than individual inserts)
        insert_buffer = []
        for item in anime_data:
            # Extract TMDB ID from nested dictionary
            tmdb_id_raw = item.get("themoviedb_id")
            tmdb_id = None
            tmdb_season = 1  #TODO: Default fallback for now, I should implement some parsing logic here, though.
            
            if isinstance(tmdb_id_raw, dict):
                # It could be {"tv": 123} or {"movie": 123}
                tmdb_id = tmdb_id_raw.get("tv") or tmdb_id_raw.get("movie")
            elif isinstance(tmdb_id_raw, int):
                tmdb_id = tmdb_id_raw

            # --- FIX 2: Extract TMDB Season if it exists ---
            season_raw = item.get("season")
            if isinstance(season_raw, dict):
                # Safely grabs the 'tmdb' season, defaults to 1 if missing
                tmdb_season = season_raw.get("tmdb", 1)

            # Extract other flat IDs safely
            anilist_id = item.get("anilist_id")
            mal_id = item.get("mal_id")
            
            # Extract title (Fribb uses standard 'title' or fallback keys)
            title = item.get("title")

            # Only append to block if we managed to successfully extract both core IDs
            if tmdb_id and anilist_id: 
                insert_buffer.append((int(tmdb_id), int(tmdb_season), int(anilist_id), mal_id, title))

        # Bulk upsert using SQLite's conflict resolution
        cursor.executemany('''
            INSERT OR REPLACE INTO mappings (tmdb_id, tmdb_season, anilist_id, mal_id, title_romaji)
            VALUES (?, ?, ?, ?, ?)
        ''', insert_buffer)

        # Update our ETag metadata so we don't download it again next time
        cursor.execute("INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('etag', ?)", (new_etag,))
        
        conn.commit()
        conn.close()
        print(f"Sync complete! Successfully processed {len(insert_buffer)} records.")

# To run the script standalone:
if __name__ == "__main__":
    asyncio.run(sync_database())