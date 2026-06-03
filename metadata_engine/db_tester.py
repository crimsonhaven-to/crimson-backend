import sqlite3

DB_NAME = "anime_mappings.db"

try:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # The Query
    cursor.execute("SELECT anilist_id FROM mappings WHERE tmdb_id = ? AND tmdb_season = ?", (119495, 1))
    row = cursor.fetchone()
    
    if row:
        print(f"SUCCESS: Found AniList ID for Eminence in Shadow: {row[0]}")
    else:
        print("FAILURE: No mapping found in the local database for these parameters.")
finally:
    conn.close()