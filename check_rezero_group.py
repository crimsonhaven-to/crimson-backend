import sqlite3
conn = sqlite3.connect("anime_mappings.db")
cursor = conn.cursor()
cursor.execute("SELECT * FROM season_groups WHERE group_id = 876 ORDER BY season_number")
for row in cursor.fetchall():
    print(row)
conn.close()
