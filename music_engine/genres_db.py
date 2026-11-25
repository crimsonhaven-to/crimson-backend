"""Postgres access for genres (migrations/015_music_genres.sql)."""

from __future__ import annotations

from typing import Optional

from psycopg.types.json import Jsonb

from core.clock import utc_now_iso
from core.db_pool import get_connection

# A track's genres as a sorted JSON list: its own, plus those of its Spotify
# artists. Selected next to ``t.*`` wherever the library is listed.
TRACK_GENRES_T = """
    (SELECT COALESCE(JSONB_AGG(g.genre ORDER BY g.genre), '[]'::jsonb) FROM (
        SELECT JSONB_ARRAY_ELEMENTS_TEXT(t.genres) AS genre
        UNION
        SELECT JSONB_ARRAY_ELEMENTS_TEXT(a.genres) FROM music_artists a
        WHERE a.spotify_id IN (SELECT JSONB_ARRAY_ELEMENTS_TEXT(t.spotify_artist_ids))
    ) g) AS genres
"""


def track_genres(track_id: int) -> list[str]:
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {TRACK_GENRES_T} FROM music_tracks t WHERE t.id = %s", (track_id,)
        ).fetchone()
    return row["genres"] if row else []


def lookup_user() -> Optional[int]:
    """Whose Spotify link answers the lookups. Artists and their genres are the
    same for everyone, so any working link will do; the freshest is likeliest
    to still work."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM music_spotify_links ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return row["user_id"] if row else None


def unknown_artist_ids(limit: int) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ids.spotify_id
            FROM music_tracks t
            CROSS JOIN JSONB_ARRAY_ELEMENTS_TEXT(t.spotify_artist_ids) AS ids(spotify_id)
            WHERE NOT EXISTS (
                SELECT 1 FROM music_artists a WHERE a.spotify_id = ids.spotify_id
            )
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [row["spotify_id"] for row in rows]


def save_artist(spotify_id: str, name: str, genres: list[str]) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO music_artists (spotify_id, name, genres, fetched_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (spotify_id) DO UPDATE SET
                name = EXCLUDED.name, genres = EXCLUDED.genres, fetched_at = EXCLUDED.fetched_at
            """,
            (spotify_id, name, Jsonb(genres), utc_now_iso()),
        )


def tracks_without_artist_ids(limit: int) -> list[dict]:
    """Ready songs no import gave artist ids: added by search, or from a CSV.
    Only ready ones, because a search result's artist is a guess until the
    download replaces it with the source's own."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT id, artists FROM music_tracks
            WHERE status = 'ready' AND spotify_artist_ids = '[]'::jsonb
                  AND artists_checked_at IS NULL AND artists <> '[]'::jsonb
            ORDER BY id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()


def set_artist_ids(track_id: int, spotify_artist_ids: list[str]) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE music_tracks SET spotify_artist_ids = %s, artists_checked_at = %s
            WHERE id = %s
            """,
            (Jsonb(spotify_artist_ids), utc_now_iso(), track_id),
        )
