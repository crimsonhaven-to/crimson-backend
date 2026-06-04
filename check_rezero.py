import sqlite3
conn = sqlite3.connect("anime_mappings.db")
cursor = conn.cursor()
print("Mappings for Re:Zero Season 2 (AniList ID 108632):")
cursor.execute("SELECT * FROM mappings WHERE anilist_id = 108632")
for row in cursor.fetchall():
    print(row)

print("\nSeason groups for Re:Zero Season 2:")
cursor.execute("SELECT * FROM season_groups WHERE anilist_id = 108632")
for row in cursor.fetchall():
    print(row)
conn.close()
