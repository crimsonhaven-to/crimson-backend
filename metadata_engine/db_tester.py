"""Quick sanity check for the mapping database.

Run after a sync to confirm the schema is populated and multi-season shows
resolve to distinct AniList ids per season. Reads through the shared PostgreSQL
pool, so DATABASE_URL / POSTGRES_* must point at the same database the API uses.

    python -m metadata_engine.db_tester
"""

from dotenv import load_dotenv

from db_pool import get_connection

load_dotenv()


def main():
    with get_connection() as conn:
        cursor = conn.cursor()
        for table in ("anime_entries", "tmdb_seasons", "tmdb_extras", "tmdb_shows"):
            cursor.execute(f"SELECT COUNT(*) AS n FROM {table}")
            print(f"{table:>14}: {cursor.fetchone()['n']} rows")

        # Show a few multi-season examples (proof the collapse bug is fixed).
        print("\nSample multi-season shows (tmdb_id -> seasons):")
        cursor.execute("""
            SELECT tmdb_id, COUNT(*) AS season_count
            FROM tmdb_seasons
            GROUP BY tmdb_id
            HAVING COUNT(*) >= 3
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
                WHERE s.tmdb_id = %s
                ORDER BY s.season_number
                """,
                (tmdb_id,),
            )
            seasons = cursor.fetchall()
            print(f"  TMDB {tmdb_id} ({row['season_count']} seasons):")
            for s in seasons:
                print(f"    S{s['season_number']:<2} anilist={s['anilist_id']} {s['title_romaji']}")


if __name__ == "__main__":
    main()
