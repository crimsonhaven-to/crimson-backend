"""
Storage for the airing calendar, subscriptions and the notification ledger.

Every method here is synchronous psycopg, like the other stores, so callers
either use a plain ``def`` handler or wrap in ``run_in_threadpool``.

The schema lives in ``migrations/004_airing.sql``, not in an ``init_db()`` here:
everything from version 0 onward is a numbered migration (see
``migrations/000_baseline.sql``), the same way ``chat_engine`` ships none.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from core.db_pool import get_connection
from core.clock import utc_now

logger = logging.getLogger("crimson.airing")

# A schedule row older than this is neither news nor calendar, so the sweep drops
# it. Comfortably longer than the notify lookback, so a row is never pruned while
# it could still produce a notification.
SCHEDULE_RETENTION_DAYS = 30

# The ledger has to outlive the schedule rows it refers to, or pruning the
# schedule and then re-fetching it would make an already-sent episode look
# unsent. A season is thirteen weeks; a year covers a rewatch of a long run.
LEDGER_RETENTION_DAYS = 365


class AiringStore:
    def _connect(self):
        return get_connection()

    # -- schedule -------------------------------------------------------
    def upsert_schedule(
        self, rows: Sequence[Tuple[int, int, datetime, Optional[str]]]
    ) -> int:
        """Record airings as ``(anilist_id, episode, airing_at, title)``.

        Upserts on ``(anilist_id, episode)`` so a delayed broadcast moves its row
        rather than adding a second one. Returns the number written.
        """
        if not rows:
            return 0
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT INTO airing_schedule (anilist_id, episode, airing_at, fetched_at, title)
                VALUES (%s, %s, %s, now(), %s)
                ON CONFLICT (anilist_id, episode) DO UPDATE
                    SET airing_at = EXCLUDED.airing_at,
                        fetched_at = EXCLUDED.fetched_at,
                        -- A refresh that came back without a name must not erase
                        -- the one already on the row.
                        title = COALESCE(EXCLUDED.title, airing_schedule.title)
                """,
                list(rows),
            )
        return len(rows)

    def calendar(self, start: datetime, end: datetime, user_id: Optional[int] = None) -> List[Dict]:
        """Airings in ``[start, end)``, each flagged with whether the caller follows it.

        One query for both halves of the calendar: the client shows everything
        airing and highlights the caller's own, so filtering server-side to the
        subscribed rows would need a second request to draw the rest.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.anilist_id,
                       a.episode,
                       a.airing_at,
                       s.user_id IS NOT NULL AS subscribed,
                       s.title,
                       s.poster,
                       a.title AS schedule_title,
                       e.title_english,
                       e.title_romaji
                FROM airing_schedule a
                LEFT JOIN anime_subscriptions s
                       ON s.anilist_id = a.anilist_id AND s.user_id = %(user_id)s
                LEFT JOIN anime_entries e
                       ON e.anilist_id = a.anilist_id
                WHERE a.airing_at >= %(start)s AND a.airing_at < %(end)s
                ORDER BY a.airing_at, a.anilist_id
                """,
                {"user_id": user_id, "start": start, "end": end},
            ).fetchall()

        return [
            {
                "anilist_id": r["anilist_id"],
                "episode": r["episode"],
                "airing_at": r["airing_at"].isoformat(),
                "subscribed": bool(r["subscribed"]),
                # The subscription's snapshot first (what the user saw when they
                # followed it), then AniList's own name from the schedule fetch,
                # then the catalogue's. anime_entries comes last because the Fribb
                # resync that fills it lags a new season, which is exactly when a
                # title is most worth following.
                "title": (r["title"] or r["schedule_title"]
                          or r["title_english"] or r["title_romaji"]),
                "poster": r["poster"],
            }
            for r in rows
        ]

    def subscribed_ids(self) -> List[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT anilist_id FROM anime_subscriptions ORDER BY anilist_id"
            ).fetchall()
        return [r["anilist_id"] for r in rows]

    # -- subscriptions --------------------------------------------------
    def list_subscriptions(self, user_id: int) -> List[Dict]:
        """The caller's subscriptions, each with its next scheduled episode."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.anilist_id,
                       s.title,
                       s.poster,
                       s.notify_email,
                       s.created_at,
                       n.episode   AS next_episode,
                       n.airing_at AS next_airing_at,
                       t.title     AS schedule_title
                FROM anime_subscriptions s
                LEFT JOIN LATERAL (
                    SELECT episode, airing_at
                    FROM airing_schedule
                    WHERE anilist_id = s.anilist_id AND airing_at > now()
                    ORDER BY airing_at
                    LIMIT 1
                ) n ON TRUE
                -- A separate lookup from the one above: a title between seasons
                -- has no upcoming episode but still has a name on its past rows,
                -- and a follow with no name at all is unmanageable.
                LEFT JOIN LATERAL (
                    SELECT title
                    FROM airing_schedule
                    WHERE anilist_id = s.anilist_id AND title IS NOT NULL
                    ORDER BY airing_at DESC
                    LIMIT 1
                ) t ON TRUE
                WHERE s.user_id = %s
                ORDER BY s.created_at DESC
                """,
                (user_id,),
            ).fetchall()

        return [
            {
                "anilist_id": r["anilist_id"],
                "title": r["title"] or r["schedule_title"],
                "poster": r["poster"],
                "notify_email": r["notify_email"],
                "created_at": r["created_at"].isoformat(),
                "next_episode": r["next_episode"],
                "next_airing_at": r["next_airing_at"].isoformat() if r["next_airing_at"] else None,
            }
            for r in rows
        ]

    def subscribe(self, user_id: int, anilist_id: int, title: Optional[str],
                  poster: Optional[str], notify_email: bool = True) -> None:
        """Follow a title, or update the notification preference on an existing follow."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO anime_subscriptions (user_id, anilist_id, title, poster, notify_email)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, anilist_id) DO UPDATE
                    SET notify_email = EXCLUDED.notify_email,
                        -- Only overwrite the snapshot when the caller supplied
                        -- one, so re-subscribing from a context that has no title
                        -- cannot blank a good one.
                        title  = COALESCE(EXCLUDED.title, anime_subscriptions.title),
                        poster = COALESCE(EXCLUDED.poster, anime_subscriptions.poster)
                """,
                (user_id, anilist_id, title, poster, notify_email),
            )

    def unsubscribe(self, user_id: int, anilist_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM anime_subscriptions WHERE user_id = %s AND anilist_id = %s",
                (user_id, anilist_id),
            )
            return bool(cursor.rowcount)

    # -- the notification ledger ----------------------------------------
    def pending_notifications(self, lookback_hours: int, limit: int) -> List[Dict]:
        """Episodes that have aired and whose subscriber has not been told yet.

        Bounded at both ends on purpose. ``airing_at <= now()`` is the obvious
        half; ``airing_at > now() - lookback`` is the half that matters
        operationally, because without it the first run after enabling the
        feature would mail every subscriber about every episode in the table, and
        a subscriber who verified their address today would receive a backlog.
        Older than the lookback is not news.

        Only accounts with a verified email are considered: a mnemonic account
        has no address at all, and mailing an unverified one sends mail to
        somebody who has not confirmed they own it.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.user_id,
                       s.anilist_id,
                       s.title,
                       a.episode,
                       a.airing_at,
                       acc.email,
                       acc.username
                FROM airing_schedule a
                JOIN anime_subscriptions s ON s.anilist_id = a.anilist_id
                JOIN accounts acc          ON acc.user_id = s.user_id
                LEFT JOIN airing_notifications n
                       ON n.user_id = s.user_id
                      AND n.anilist_id = a.anilist_id
                      AND n.episode = a.episode
                WHERE s.notify_email
                  AND acc.email IS NOT NULL
                  AND acc.email_verified
                  AND a.airing_at <= now()
                  AND a.airing_at > now() - make_interval(hours => %(lookback)s)
                  AND n.user_id IS NULL
                ORDER BY a.airing_at DESC
                LIMIT %(limit)s
                """,
                {"lookback": lookback_hours, "limit": limit},
            ).fetchall()
        return [dict(r) for r in rows]

    def claim(self, user_id: int, anilist_id: int, episode: int) -> bool:
        """Take ownership of one notification. True if this caller won it.

        The claim is the INSERT itself, so two replicas racing on the same row
        cannot both send: the loser's rowcount is 0. Always before the send,
        never after.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO airing_notifications (user_id, anilist_id, episode, status)
                VALUES (%s, %s, %s, 'claimed')
                ON CONFLICT (user_id, anilist_id, episode) DO NOTHING
                """,
                (user_id, anilist_id, episode),
            )
            return bool(cursor.rowcount)

    def record_outcome(self, user_id: int, anilist_id: int, episode: int, sent: bool) -> None:
        """Mark a claimed notification sent or failed.

        A failure is recorded, never un-claimed. Deleting the row to retry would
        reopen the resend hole the claim exists to close, and a mail nobody can
        deliver is better lost than sent repeatedly.
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE airing_notifications
                   SET status = %s, sent_at = %s
                 WHERE user_id = %s AND anilist_id = %s AND episode = %s
                """,
                ("sent" if sent else "failed", utc_now() if sent else None,
                 user_id, anilist_id, episode),
            )

    # -- retention ------------------------------------------------------
    def purge_old(self) -> Dict[str, int]:
        """Drop schedule rows and ledger rows past their retention."""
        removed = {"schedule": 0, "notifications": 0}
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM airing_schedule WHERE airing_at < %s",
                    (utc_now() - timedelta(days=SCHEDULE_RETENTION_DAYS),),
                )
                removed["schedule"] = cursor.rowcount or 0
                cursor.execute(
                    "DELETE FROM airing_notifications WHERE claimed_at < %s",
                    (utc_now() - timedelta(days=LEDGER_RETENTION_DAYS),),
                )
                removed["notifications"] = cursor.rowcount or 0
        except Exception as e:
            logger.error(f"Airing retention sweep failed: {e}")
        return removed

    def stats(self) -> Dict[str, int]:
        """Counts for the admin system panel and the boot log."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM airing_schedule)                          AS scheduled,
                           (SELECT COUNT(*) FROM anime_subscriptions)                      AS subscriptions,
                           (SELECT COUNT(*) FROM airing_notifications WHERE status = 'sent')   AS sent,
                           (SELECT COUNT(*) FROM airing_notifications WHERE status = 'failed') AS failed
                    """
                ).fetchone()
            return dict(row)
        except Exception as e:
            logger.error(f"Airing stats failed: {e}")
            return {"scheduled": 0, "subscriptions": 0, "sent": 0, "failed": 0}


store = AiringStore()
