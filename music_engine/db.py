"""Postgres store for the music library. The schema is migrations/008_music.sql,
which also documents the tables and the track lifecycle."""

from __future__ import annotations

from typing import Callable, Optional

from psycopg.types.json import Jsonb

from core.clock import utc_now_iso
from core.db_pool import get_connection

from .provider import Candidate, ImportedTrack

STATUS_PENDING = "pending"
STATUS_WORKING = "working"
STATUS_READY = "ready"
STATUS_REVIEW = "review"
STATUS_UNMATCHED = "unmatched"
STATUS_FAILED = "failed"

# A retryable failure goes back to pending this many times before it sticks.
MAX_ATTEMPTS = 3

_TRACK_COLS = (
    "id, track_key, spotify_id, isrc, title, artists, album, album_artist, track_number, "
    "disc_number, release_date, duration_ms, cover_url, status, match_url, match_manual, "
    "rel_path, cover_path, file_size, attempts, error, tags_from_source, mirrored_at, created_at, "
    "updated_at"
)
_TRACK_COLS_T = ", ".join("t." + c for c in _TRACK_COLS.split(", "))
_PLAYLIST_COLS = (
    "id, user_id, source, spotify_id, name, description, cover_url, snapshot_id, "
    "sync_enabled, last_synced_at, last_error, created_at, updated_at"
)
_PLAYLIST_COLS_P = ", ".join("p." + c for c in _PLAYLIST_COLS.split(", "))


class MusicStore:
    # --- access and the Spotify link ------------------------------------------
    def set_music_access(self, user_id: int, enabled: bool) -> None:
        with get_connection() as conn:
            conn.execute(
                "UPDATE accounts SET music_enabled = %s WHERE user_id = %s", (enabled, user_id)
            )

    def get_link(self, user_id: int) -> Optional[dict]:
        with get_connection() as conn:
            return conn.execute(
                "SELECT * FROM music_spotify_links WHERE user_id = %s", (user_id,)
            ).fetchone()

    def save_link(
        self,
        user_id: int,
        *,
        client_id: str,
        refresh_token: str,
        access_token: str,
        access_expires_at: str,
        scope: str,
        spotify_user_id: Optional[str],
        display_name: Optional[str],
    ) -> None:
        now = utc_now_iso()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO music_spotify_links
                    (user_id, client_id, refresh_token, access_token, access_expires_at, scope,
                     spotify_user_id, display_name, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    client_id = EXCLUDED.client_id,
                    refresh_token = EXCLUDED.refresh_token,
                    access_token = EXCLUDED.access_token,
                    access_expires_at = EXCLUDED.access_expires_at,
                    scope = EXCLUDED.scope,
                    spotify_user_id = EXCLUDED.spotify_user_id,
                    display_name = EXCLUDED.display_name,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    user_id, client_id, refresh_token, access_token, access_expires_at, scope,
                    spotify_user_id, display_name, now, now,
                ),
            )

    def delete_link(self, user_id: int) -> None:
        with get_connection() as conn:
            conn.execute("DELETE FROM music_spotify_links WHERE user_id = %s", (user_id,))

    def refresh_link(
        self,
        user_id: int,
        still_valid: Callable[[dict], bool],
        refresh: Callable[[dict], dict],
    ) -> Optional[dict]:
        """Refresh under a row lock. Spotify rotates the refresh token on use, so
        two replicas refreshing at once would leave the loser holding a dead
        token; the lock makes the second one find the first one's result."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM music_spotify_links WHERE user_id = %s FOR UPDATE", (user_id,)
            ).fetchone()
            if row is None or still_valid(row):
                return row
            tokens = refresh(row)
            return conn.execute(
                """
                UPDATE music_spotify_links
                SET access_token = %s, access_expires_at = %s, refresh_token = %s, updated_at = %s
                WHERE user_id = %s
                RETURNING *
                """,
                (
                    tokens["access_token"], tokens["access_expires_at"], tokens["refresh_token"],
                    utc_now_iso(), user_id,
                ),
            ).fetchone()

    # --- playlists --------------------------------------------------------------
    def list_playlists(self, user_id: int) -> list[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"""
                SELECT {_PLAYLIST_COLS_P},
                       COUNT(pt.track_id) FILTER (WHERE NOT pt.removed_upstream) AS track_count,
                       COUNT(pt.track_id) FILTER (WHERE pt.removed_upstream) AS removed_count,
                       COUNT(pt.track_id) FILTER (WHERE t.status = 'ready') AS ready_count,
                       COUNT(pt.track_id) FILTER (WHERE t.status = 'review') AS review_count,
                       COUNT(pt.track_id) FILTER (
                           WHERE t.status IN ('unmatched', 'failed')) AS problem_count,
                       COALESCE(SUM(t.duration_ms), 0) AS duration_ms
                FROM music_playlists p
                LEFT JOIN music_playlist_tracks pt ON pt.playlist_id = p.id
                LEFT JOIN music_tracks t ON t.id = pt.track_id
                WHERE p.user_id = %s
                GROUP BY p.id
                ORDER BY p.created_at, p.id
                """,
                (user_id,),
            ).fetchall()

    def get_playlist(self, user_id: int, playlist_id: int) -> Optional[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"SELECT {_PLAYLIST_COLS} FROM music_playlists WHERE id = %s AND user_id = %s",
                (playlist_id, user_id),
            ).fetchone()

    def get_playlist_by_id(self, playlist_id: int) -> Optional[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"SELECT {_PLAYLIST_COLS} FROM music_playlists WHERE id = %s", (playlist_id,)
            ).fetchone()

    def upsert_playlist(
        self,
        user_id: int,
        *,
        source: str,
        spotify_id: Optional[str],
        name: str,
        description: str,
        cover_url: Optional[str],
        sync_enabled: bool,
    ) -> dict:
        now = utc_now_iso()
        with get_connection() as conn:
            return conn.execute(
                f"""
                INSERT INTO music_playlists
                    (user_id, source, spotify_id, name, description, cover_url, sync_enabled,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, source, spotify_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    cover_url = COALESCE(EXCLUDED.cover_url, music_playlists.cover_url),
                    updated_at = EXCLUDED.updated_at
                RETURNING {_PLAYLIST_COLS}
                """,
                (
                    user_id, source, spotify_id, name, description, cover_url, sync_enabled,
                    now, now,
                ),
            ).fetchone()

    def update_playlist(
        self, user_id: int, playlist_id: int, *, sync_enabled: bool
    ) -> Optional[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"""
                UPDATE music_playlists SET sync_enabled = %s, updated_at = %s
                WHERE id = %s AND user_id = %s
                RETURNING {_PLAYLIST_COLS}
                """,
                (sync_enabled, utc_now_iso(), playlist_id, user_id),
            ).fetchone()

    def delete_playlist(self, user_id: int, playlist_id: int) -> bool:
        """The playlist only. Its tracks and their files stay in the library."""
        with get_connection() as conn:
            row = conn.execute(
                "DELETE FROM music_playlists WHERE id = %s AND user_id = %s RETURNING id",
                (playlist_id, user_id),
            ).fetchone()
        return row is not None

    def mark_synced(
        self, playlist_id: int, *, snapshot_id: Optional[str], error: Optional[str]
    ) -> None:
        """A failed sync still stamps the time, so a broken playlist is retried on
        the next interval rather than on every worker tick."""
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE music_playlists
                SET last_synced_at = %s, last_error = %s,
                    snapshot_id = COALESCE(%s, snapshot_id)
                WHERE id = %s
                """,
                (utc_now_iso(), error[:500] if error else None, snapshot_id, playlist_id),
            )

    def due_for_sync(self, synced_before: str, limit: int = 10) -> list[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"""
                SELECT {_PLAYLIST_COLS} FROM music_playlists
                WHERE sync_enabled AND spotify_id IS NOT NULL
                      AND (last_synced_at IS NULL OR last_synced_at < %s)
                ORDER BY last_synced_at NULLS FIRST, id
                LIMIT %s
                """,
                (synced_before, limit),
            ).fetchall()

    def playlist_tracks(self, playlist_id: int) -> list[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"""
                SELECT {_TRACK_COLS_T}, pt.position, pt.added_at, pt.removed_upstream
                FROM music_playlist_tracks pt
                JOIN music_tracks t ON t.id = pt.track_id
                WHERE pt.playlist_id = %s
                ORDER BY pt.removed_upstream, pt.position
                """,
                (playlist_id,),
            ).fetchall()

    def save_tracks(self, playlist_id: int, entries: list[tuple[str, ImportedTrack]]) -> dict:
        """Upsert every ``(track_key, track)``, point the playlist at them in
        order, and flag what the source no longer lists. One transaction, so a
        failed import leaves the playlist as it was.

        Metadata already on a track wins, except where it is empty: the first
        import decided the file's tags and path, and a later import must not
        rename a file under a playlist that is playing it."""
        now = utc_now_iso()
        added = 0
        with get_connection() as conn:
            before = {
                r["track_id"]
                for r in conn.execute(
                    "SELECT track_id FROM music_playlist_tracks "
                    "WHERE playlist_id = %s AND NOT removed_upstream",
                    (playlist_id,),
                ).fetchall()
            }
            seen: list[int] = []
            for position, (key, track) in enumerate(entries):
                track_id = conn.execute(
                    """
                    INSERT INTO music_tracks
                        (track_key, spotify_id, isrc, title, artists, album, album_artist,
                         track_number, disc_number, release_date, duration_ms, cover_url,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (track_key) DO UPDATE SET
                        spotify_id = COALESCE(music_tracks.spotify_id, EXCLUDED.spotify_id),
                        isrc = COALESCE(music_tracks.isrc, EXCLUDED.isrc),
                        album = CASE WHEN music_tracks.album = '' THEN EXCLUDED.album
                                     ELSE music_tracks.album END,
                        album_artist = CASE WHEN music_tracks.album_artist = ''
                                            THEN EXCLUDED.album_artist
                                            ELSE music_tracks.album_artist END,
                        track_number = COALESCE(music_tracks.track_number, EXCLUDED.track_number),
                        disc_number = COALESCE(music_tracks.disc_number, EXCLUDED.disc_number),
                        release_date = COALESCE(music_tracks.release_date, EXCLUDED.release_date),
                        duration_ms = CASE WHEN music_tracks.duration_ms = 0
                                           THEN EXCLUDED.duration_ms
                                           ELSE music_tracks.duration_ms END,
                        cover_url = COALESCE(music_tracks.cover_url, EXCLUDED.cover_url)
                    RETURNING id
                    """,
                    (
                        key, track.spotify_id, track.isrc, track.title, Jsonb(track.artists),
                        track.album, track.album_artist, track.track_number, track.disc_number,
                        track.release_date, track.duration_ms, track.cover_url, now, now,
                    ),
                ).fetchone()["id"]
                if track_id in seen:
                    continue
                seen.append(track_id)
                if track_id not in before:
                    added += 1
                conn.execute(
                    """
                    INSERT INTO music_playlist_tracks
                        (playlist_id, track_id, position, added_at, removed_upstream)
                    VALUES (%s, %s, %s, %s, FALSE)
                    ON CONFLICT (playlist_id, track_id) DO UPDATE SET
                        position = EXCLUDED.position,
                        added_at = COALESCE(EXCLUDED.added_at, music_playlist_tracks.added_at),
                        removed_upstream = FALSE
                    """,
                    (playlist_id, track_id, position, track.added_at),
                )
            removed = conn.execute(
                """
                UPDATE music_playlist_tracks SET removed_upstream = TRUE
                WHERE playlist_id = %s AND NOT removed_upstream AND NOT (track_id = ANY(%s))
                RETURNING track_id
                """,
                (playlist_id, seen),
            ).fetchall()
            conn.execute(
                "UPDATE music_playlists SET updated_at = %s WHERE id = %s", (now, playlist_id)
            )
        return {"total": len(seen), "added": added, "removed": len(removed)}

    def add_song(self, playlist_id: int, key: str, track: ImportedTrack, url: str) -> dict:
        """Append a song found by search. The recording may already be in the
        library under a Spotify import's better metadata; that row is reused.
        Returns ``track_id`` and whether the playlist gained it."""
        now = utc_now_iso()
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id FROM music_tracks WHERE match_url = %s
                ORDER BY status = 'ready' DESC, id LIMIT 1
                """,
                (url,),
            ).fetchone()
            if existing:
                track_id = existing["id"]
            else:
                track_id = conn.execute(
                    """
                    INSERT INTO music_tracks
                        (track_key, title, artists, duration_ms, cover_url, match_url,
                         match_manual, tags_from_source, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE, %s, %s)
                    ON CONFLICT (track_key) DO UPDATE SET updated_at = EXCLUDED.updated_at
                    RETURNING id
                    """,
                    (key, track.title, Jsonb(track.artists), track.duration_ms, track.cover_url,
                     url, now, now),
                ).fetchone()["id"]
            added = conn.execute(
                """
                INSERT INTO music_playlist_tracks (playlist_id, track_id, position, added_at)
                SELECT %s, %s, COALESCE(MAX(position) + 1, 0), %s
                FROM music_playlist_tracks WHERE playlist_id = %s
                ON CONFLICT (playlist_id, track_id) DO NOTHING
                RETURNING track_id
                """,
                (playlist_id, track_id, now, playlist_id),
            ).fetchone()
            conn.execute(
                "UPDATE music_playlists SET updated_at = %s WHERE id = %s", (now, playlist_id)
            )
        return {"track_id": track_id, "added": added is not None}

    def remove_song(self, playlist_id: int, track_id: int) -> bool:
        """The playlist entry only; the song and its file stay in the library."""
        with get_connection() as conn:
            row = conn.execute(
                "DELETE FROM music_playlist_tracks WHERE playlist_id = %s AND track_id = %s "
                "RETURNING track_id",
                (playlist_id, track_id),
            ).fetchone()
        return row is not None

    # --- tracks -----------------------------------------------------------------
    def get_track(self, track_id: int) -> Optional[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"SELECT {_TRACK_COLS}, candidates FROM music_tracks WHERE id = %s", (track_id,)
            ).fetchone()

    def user_has_track(self, user_id: int, track_id: int) -> bool:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM music_playlist_tracks pt
                JOIN music_playlists p ON p.id = pt.playlist_id
                WHERE p.user_id = %s AND pt.track_id = %s
                LIMIT 1
                """,
                (user_id, track_id),
            ).fetchone()
        return row is not None

    def queue_counts(self, user_id: int) -> dict:
        """Tracks in this user's playlists by status, and how many of them are in
        the CDN copy, for the library header."""
        out = {s: 0 for s in (STATUS_PENDING, STATUS_WORKING, STATUS_READY, STATUS_REVIEW,
                              STATUS_UNMATCHED, STATUS_FAILED, "mirrored")}
        with get_connection() as conn:
            for row in conn.execute(
                """
                SELECT t.status, COUNT(DISTINCT t.id) AS n,
                       COUNT(DISTINCT t.id) FILTER (WHERE t.mirrored_at IS NOT NULL) AS mirrored
                FROM music_tracks t
                JOIN music_playlist_tracks pt ON pt.track_id = t.id
                JOIN music_playlists p ON p.id = pt.playlist_id
                WHERE p.user_id = %s
                GROUP BY t.status
                """,
                (user_id,),
            ).fetchall():
                out[row["status"]] = row["n"]
                out["mirrored"] += row["mirrored"]
        return out

    def fetch_pending(self, limit: int) -> list[dict]:
        """Fewest attempts first, so one stubborn track cannot starve the queue.
        Only tracks some playlist still holds: removing a mistaken import stops
        its downloads."""
        with get_connection() as conn:
            return conn.execute(
                f"""
                SELECT {_TRACK_COLS} FROM music_tracks t
                WHERE status = %s
                      AND EXISTS (SELECT 1 FROM music_playlist_tracks pt WHERE pt.track_id = t.id)
                ORDER BY attempts, created_at, id
                LIMIT %s
                """,
                (STATUS_PENDING, limit),
            ).fetchall()

    def claim(self, track_id: int) -> bool:
        """``pending`` to ``working``; False when another worker got there first."""
        with get_connection() as conn:
            row = conn.execute(
                """
                UPDATE music_tracks SET status = %s, attempts = attempts + 1, updated_at = %s
                WHERE id = %s AND status = %s
                RETURNING id
                """,
                (STATUS_WORKING, utc_now_iso(), track_id, STATUS_PENDING),
            ).fetchone()
        return row is not None

    def set_matched(self, track_id: int, url: str, candidates: list[Candidate]) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE music_tracks SET match_url = %s, candidates = %s, updated_at = %s
                WHERE id = %s
                """,
                (url, Jsonb(_candidates_json(candidates)), utc_now_iso(), track_id),
            )

    def mark_ready(
        self,
        track_id: int,
        *,
        rel_path: str,
        cover_path: Optional[str],
        file_size: int,
        album: str,
        title: str,
        artists: list[str],
    ) -> None:
        """``title`` and ``artists`` are what the file was tagged with, which
        differs from the row only for a song added by search."""
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE music_tracks
                SET status = %s, rel_path = %s, cover_path = %s, file_size = %s, error = NULL,
                    album = CASE WHEN album = '' THEN %s ELSE album END,
                    title = %s, artists = %s, mirrored_at = NULL, updated_at = %s
                WHERE id = %s
                """,
                (STATUS_READY, rel_path, cover_path, file_size, album, title, Jsonb(artists),
                 utc_now_iso(), track_id),
            )

    def mark_review(self, track_id: int, candidates: list[Candidate]) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE music_tracks SET status = %s, candidates = %s, error = NULL, updated_at = %s
                WHERE id = %s
                """,
                (STATUS_REVIEW, Jsonb(_candidates_json(candidates)), utc_now_iso(), track_id),
            )

    def mark_failed(self, track_id: int, error: str, *, status: str = STATUS_FAILED) -> None:
        with get_connection() as conn:
            conn.execute(
                "UPDATE music_tracks SET status = %s, error = %s, updated_at = %s WHERE id = %s",
                (status, (error or "")[:500], utc_now_iso(), track_id),
            )

    def requeue(self, track_id: int, error: str) -> None:
        """A retryable failure: back to pending until MAX_ATTEMPTS, then failed."""
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE music_tracks
                SET status = CASE WHEN attempts >= %s THEN %s ELSE %s END,
                    error = %s, updated_at = %s
                WHERE id = %s
                """,
                (MAX_ATTEMPTS, STATUS_FAILED, STATUS_PENDING, (error or "")[:500],
                 utc_now_iso(), track_id),
            )

    def choose_match(self, track_id: int, url: str) -> None:
        """A person's pick. It never gets second-guessed by the matcher, and a
        ready track picked again is downloaded again."""
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE music_tracks
                SET match_url = %s, match_manual = TRUE, status = %s, attempts = 0, error = NULL,
                    updated_at = %s
                WHERE id = %s
                """,
                (url, STATUS_PENDING, utc_now_iso(), track_id),
            )

    def retry(self, track_id: int) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE music_tracks SET status = %s, attempts = 0, error = NULL, updated_at = %s
                WHERE id = %s AND status IN (%s, %s, %s)
                """,
                (STATUS_PENDING, utc_now_iso(), track_id, STATUS_FAILED, STATUS_UNMATCHED,
                 STATUS_REVIEW),
            )

    def reset_stale(self) -> int:
        """On worker start, put every ``working`` row back. Safe because the
        music-worker deploys stop-first, so no peer holds one."""
        with get_connection() as conn:
            rows = conn.execute(
                """
                UPDATE music_tracks SET status = %s, updated_at = %s WHERE status = %s
                RETURNING id
                """,
                (STATUS_PENDING, utc_now_iso(), STATUS_WORKING),
            ).fetchall()
        return len(rows)

    def fetch_unmirrored(self, limit: int) -> list[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"""
                SELECT {_TRACK_COLS} FROM music_tracks
                WHERE status = %s AND mirrored_at IS NULL
                ORDER BY id
                LIMIT %s
                """,
                (STATUS_READY, limit),
            ).fetchall()

    def mark_mirrored(self, track_id: int, rel_path: str) -> None:
        """Only while the track still points at the file that was uploaded: a
        download that finished meanwhile left a new file to copy."""
        with get_connection() as conn:
            conn.execute(
                "UPDATE music_tracks SET mirrored_at = %s WHERE id = %s AND rel_path = %s",
                (utc_now_iso(), track_id, rel_path),
            )

    def playlists_for_track(self, track_id: int) -> list[dict]:
        with get_connection() as conn:
            return conn.execute(
                f"""
                SELECT {_PLAYLIST_COLS_P} FROM music_playlists p
                JOIN music_playlist_tracks pt ON pt.playlist_id = p.id
                WHERE pt.track_id = %s
                """,
                (track_id,),
            ).fetchall()

    def ready_paths_for_playlist(self, playlist_id: int) -> list[dict]:
        """What a playlist file lists: ready tracks in playlist order, removed
        ones included, since keeping them is the point."""
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT t.rel_path, t.title, t.artists, t.duration_ms
                FROM music_playlist_tracks pt
                JOIN music_tracks t ON t.id = pt.track_id
                WHERE pt.playlist_id = %s AND t.status = %s
                ORDER BY pt.removed_upstream, pt.position
                """,
                (playlist_id, STATUS_READY),
            ).fetchall()


def _candidates_json(candidates: list[Candidate]) -> list[dict]:
    return [
        {
            "url": c.url,
            "title": c.title,
            "channel": c.channel,
            "duration_ms": c.duration_ms,
            "thumbnail_url": c.thumbnail_url,
            "score": round(c.score, 3),
            "reasons": c.reasons,
        }
        for c in candidates[:10]
    ]


store = MusicStore()
