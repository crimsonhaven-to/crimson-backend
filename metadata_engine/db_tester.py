"""Quick sanity check for the mapping database.

Run after a sync to confirm the schema is populated and multi-season shows
resolve to distinct AniList ids per season.

    python -m metadata_engine.db_tester
"""

import sqlite3

DB_NAME = "anime_mappings.db"


def main():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        for table in ("anime_entries", "tmdb_seasons", "tmdb_extras", "tmdb_shows"):
            cursor.execute(f"SELECT COUNT(*) AS n FROM {table}")
            print(f"{table:>14}: {cursor.fetchone()['n']} rows")

        # Show a few multi-season examples (proof the collapse bug is fixed).
        print("\nSample multi-season shows (tmdb_id -> seasons):")
        cursor.execute("""
            SELECT tmdb_id, COUNT(*) AS season_count
            FROM tmdb_seasons
            GROUP BY tmdb_id
            HAVING season_count >= 3
            ORDER BY season_count DESC
            LIMIT 5
        """)
        for row in cursor.fetchall():
            tmdb_id = row["tmdb_id"]
            cursor.execute(
                """
                SELECT s.season_number, s.anilist_id, e.title_romaji
                FROM tmdb_seasons s
                LEFT JOIN anime_entries e ON s.anilist_id = e.anilist_id
                WHERE s.tmdb_id = ?
                ORDER BY s.season_number
                """,
                (tmdb_id,),
            )
            seasons = cursor.fetchall()
            print(f"  TMDB {tmdb_id} ({row['season_count']} seasons):")
            for s in seasons:
                print(f"    S{s['season_number']:<2} anilist={s['anilist_id']} {s['title_romaji']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
