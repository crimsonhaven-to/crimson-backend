"""Postgres access for music_listens (migrations/011_music_listens.sql)."""

from __future__ import annotations

from datetime import datetime, timedelta

from core.clock import utc_now
from core.db_pool import get_connection

from .db import _TRACK_COLS_T

RETENTION_DAYS = 3 * 365


def durations(track_ids: list[int]) -> dict[int, int]:
    """duration_ms per track id, for the ids that exist."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, duration_ms FROM music_tracks WHERE id = ANY(%s)", (track_ids,)
        ).fetchall()
    return {row["id"]: row["duration_ms"] for row in rows}


def insert(user_id: int, listens: list[tuple[int, datetime, float]]) -> int:
    """(track_id, listened_at, seconds) rows. A report sent twice is one row."""
    inserted = 0
    with get_connection() as conn:
        for track_id, listened_at, seconds in listens:
            cur = conn.execute(
                """
                INSERT INTO music_listens (user_id, track_id, listened_at, seconds)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, track_id, listened_at) DO NOTHING
                """,
                (user_id, track_id, listened_at, seconds),
            )
            inserted += cur.rowcount or 0
    return inserted


def between(user_id: int, lo: datetime, hi: datetime) -> list[dict]:
    """Every listen in [lo, hi), with the track row it played."""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT l.listened_at, l.seconds, {_TRACK_COLS_T}
            FROM music_listens l
            JOIN music_tracks t ON t.id = l.track_id
            WHERE l.user_id = %s AND l.listened_at >= %s AND l.listened_at < %s
            """,
            (user_id, lo, hi),
        ).fetchall()
    return [dict(r) for r in rows]


def purge_old() -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM music_listens WHERE listened_at < %s",
            (utc_now() - timedelta(days=RETENTION_DAYS),),
        )
        return cur.rowcount or 0
