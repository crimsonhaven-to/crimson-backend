"""Messaging storage: rooms, their members, messages and blocks.

The schema is ``migrations/014_member_messages.sql``. Synchronous psycopg on the
shared pool, like the other stores; the routes are plain ``def`` so FastAPI runs
them in its thread pool.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from psycopg.types.json import Jsonb

from core.db_pool import get_connection

# A long inbox is paged by nobody: the oldest quiet chats simply drop off.
INBOX_LIMIT = 200


class MessagingStore:
    # --- members -----------------------------------------------------------
    def members_by_handles(self, handles: List[str]) -> List[Dict]:
        with get_connection() as conn:
            return conn.execute(
                "SELECT user_id, handle, username FROM accounts WHERE handle = ANY(%s)",
                (handles,),
            ).fetchall()

    def has_blocked(self, user_id: int, other_id: int) -> bool:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM messaging_blocks WHERE user_id = %s AND blocked_id = %s",
                (user_id, other_id),
            ).fetchone()
        return row is not None

    def blocked_by_any(self, user_id: int, others: List[int]) -> List[int]:
        """Which of ``others`` have blocked ``user_id``."""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM messaging_blocks WHERE blocked_id = %s AND user_id = ANY(%s)",
                (user_id, others),
            ).fetchall()
        return [r["user_id"] for r in rows]

    # --- rooms -------------------------------------------------------------
    def direct_room(self, user_id: int, other_id: int) -> int:
        key = f"{min(user_id, other_id)}:{max(user_id, other_id)}"
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messaging_rooms (kind, direct_key) VALUES ('direct', %s) "
                "ON CONFLICT (direct_key) DO NOTHING",
                (key,),
            )
            room_id = conn.execute(
                "SELECT id FROM messaging_rooms WHERE direct_key = %s", (key,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO messaging_members (room_id, user_id) "
                "SELECT %s, u FROM unnest(%s::bigint[]) AS u ON CONFLICT DO NOTHING",
                (room_id, [user_id, other_id]),
            )
        return room_id

    def create_group(self, name: str, user_ids: List[int]) -> int:
        with get_connection() as conn:
            room_id = conn.execute(
                "INSERT INTO messaging_rooms (kind, name) VALUES ('group', %s) RETURNING id",
                (name,),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO messaging_members (room_id, user_id) "
                "SELECT %s, u FROM unnest(%s::bigint[]) AS u ON CONFLICT DO NOTHING",
                (room_id, user_ids),
            )
        return room_id

    def add_members(self, room_id: int, user_ids: List[int]) -> None:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messaging_members (room_id, user_id) "
                "SELECT %s, u FROM unnest(%s::bigint[]) AS u ON CONFLICT DO NOTHING",
                (room_id, user_ids),
            )

    def rename(self, room_id: int, name: str) -> None:
        with get_connection() as conn:
            conn.execute("UPDATE messaging_rooms SET name = %s WHERE id = %s", (name, room_id))

    def leave(self, room_id: int, user_id: int) -> None:
        """One transaction, so two last members leaving at once still delete the room."""
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM messaging_members WHERE room_id = %s AND user_id = %s",
                (room_id, user_id),
            )
            conn.execute(
                "DELETE FROM messaging_rooms r WHERE r.id = %s AND NOT EXISTS "
                "(SELECT 1 FROM messaging_members m WHERE m.room_id = r.id)",
                (room_id,),
            )

    def member_ids(self, room_id: int) -> List[int]:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM messaging_members WHERE room_id = %s", (room_id,)
            ).fetchall()
        return [r["user_id"] for r in rows]

    def rooms(self, user_id: int, room_id: Optional[int] = None) -> List[Dict]:
        """The caller's rooms, newest activity first, each with its members, unread
        count and last message. With ``room_id``, just that one (if a member)."""
        only_room = "AND r.id = %s" if room_id is not None else ""
        params: list = [user_id] + ([room_id] if room_id is not None else []) + [INBOX_LIMIT]
        with get_connection() as conn:
            rooms = conn.execute(
                f"""
                SELECT r.id, r.kind, r.name, r.last_message_at,
                       (SELECT COUNT(*) FROM messaging_messages x
                         WHERE x.room_id = r.id AND x.id > m.last_read_id
                           AND x.sender_id IS DISTINCT FROM m.user_id) AS unread,
                       last.id AS last_id, last.body AS last_body,
                       last.attachment AS last_attachment, last.created_at AS last_created_at,
                       last.sender_id = m.user_id AS last_mine, a.handle AS last_handle,
                       a.username AS last_username
                FROM messaging_members m
                JOIN messaging_rooms r ON r.id = m.room_id
                LEFT JOIN LATERAL (
                    SELECT id, sender_id, body, attachment, created_at
                    FROM messaging_messages WHERE room_id = r.id ORDER BY id DESC LIMIT 1
                ) last ON TRUE
                LEFT JOIN accounts a ON a.user_id = last.sender_id
                WHERE m.user_id = %s {only_room}
                ORDER BY r.last_message_at DESC
                LIMIT %s
                """,
                params,
            ).fetchall()
            if not rooms:
                return []
            members = conn.execute(
                """
                SELECT mm.room_id, a.user_id, a.handle, a.username
                FROM messaging_members mm JOIN accounts a ON a.user_id = mm.user_id
                WHERE mm.room_id = ANY(%s) ORDER BY mm.joined_at, a.user_id
                """,
                ([r["id"] for r in rooms],),
            ).fetchall()
        by_room: Dict[int, List[Dict]] = {}
        for m in members:
            by_room.setdefault(m["room_id"], []).append(m)
        return [{**r, "members": by_room.get(r["id"], [])} for r in rooms]

    # --- messages ----------------------------------------------------------
    def messages(
        self, room_id: int, user_id: int, *, after: Optional[int], before: Optional[int], limit: int
    ) -> List[Dict]:
        """Oldest first. ``after`` is the poll for new ones, otherwise the newest
        page (older than ``before`` when given)."""
        select = """
            SELECT x.id, x.body, x.attachment, x.created_at, x.sender_id = %s AS mine,
                   a.handle AS sender_handle, a.username AS sender_username
            FROM messaging_messages x LEFT JOIN accounts a ON a.user_id = x.sender_id
            WHERE x.room_id = %s
        """
        with get_connection() as conn:
            if after is not None:
                return conn.execute(
                    select + " AND x.id > %s ORDER BY x.id LIMIT %s",
                    (user_id, room_id, after, limit),
                ).fetchall()
            if before is not None:
                rows = conn.execute(
                    select + " AND x.id < %s ORDER BY x.id DESC LIMIT %s",
                    (user_id, room_id, before, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    select + " ORDER BY x.id DESC LIMIT %s", (user_id, room_id, limit)
                ).fetchall()
        return list(reversed(rows))

    def send(
        self, room_id: int, sender_id: int, body: str, attachment: Optional[Dict]
    ) -> Dict:
        """Sending also reads up to your own message."""
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO messaging_messages (room_id, sender_id, body, attachment)
                VALUES (%s, %s, %s, %s) RETURNING id, created_at
                """,
                (room_id, sender_id, body, Jsonb(attachment) if attachment else None),
            ).fetchone()
            conn.execute(
                "UPDATE messaging_rooms SET last_message_at = %s WHERE id = %s",
                (row["created_at"], room_id),
            )
            conn.execute(
                "UPDATE messaging_members SET last_read_id = %s WHERE room_id = %s AND user_id = %s",
                (row["id"], room_id, sender_id),
            )
        return row

    def mark_read(self, room_id: int, user_id: int, last_id: int) -> None:
        """Never moves backwards, and never past the newest message, so a bogus id
        cannot pre-read messages not yet sent."""
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE messaging_members SET last_read_id = GREATEST(last_read_id, LEAST(%s,
                    (SELECT COALESCE(MAX(id), 0) FROM messaging_messages WHERE room_id = %s)))
                WHERE room_id = %s AND user_id = %s
                """,
                (last_id, room_id, room_id, user_id),
            )

    def unread_total(self, user_id: int) -> int:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM messaging_members m
                JOIN messaging_messages x ON x.room_id = m.room_id AND x.id > m.last_read_id
                WHERE m.user_id = %s AND x.sender_id IS DISTINCT FROM m.user_id
                """,
                (user_id,),
            ).fetchone()
        return row["n"]

    # --- blocks ------------------------------------------------------------
    def blocks(self, user_id: int) -> List[Dict]:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT a.handle, a.username FROM messaging_blocks b
                JOIN accounts a ON a.user_id = b.blocked_id
                WHERE b.user_id = %s ORDER BY b.created_at DESC
                """,
                (user_id,),
            ).fetchall()

    def block(self, user_id: int, blocked_id: int) -> None:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messaging_blocks (user_id, blocked_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, blocked_id),
            )

    def unblock(self, user_id: int, blocked_id: int) -> None:
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM messaging_blocks WHERE user_id = %s AND blocked_id = %s",
                (user_id, blocked_id),
            )


store = MessagingStore()
