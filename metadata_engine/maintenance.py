"""Background upkeep of the ``tmdb_shows`` and ``tmdb_movies`` tables, which are
otherwise written only lazily. Runs on the single RUN_DB_SYNC replica, so one
container does this churn.

Nothing upstream signals a TMDB change the way Fribb's ETag does, so each night
the oldest 1/N of each table is re-pulled, cycling the whole table over N nights.
A backfill pages TMDB discover to fill the tables beyond what has been browsed;
the admin's button enqueues it in ``metadata_backfill_jobs``, because it runs on
a serving replica that cannot reach the portless sync container.

These jobs run in the scheduler's thread on a fresh event loop each tick, so they
open their own HTTP client rather than borrowing the main loop's shared one.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple

import httpx

from core.config import get_settings
from core.db_pool import get_connection
from core.http_client import REQUEST_TIMEOUT, fetch_with_retry

from .tmdb import discover_params, fetch_tmdb_movie, fetch_tmdb_show, keep_non_anime

logger = logging.getLogger("crimson.metadata.maintenance")

# Gentle on TMDB's rate limit and, during a bulk backfill, on standby replication.
_REFRESH_DELAY = 0.25
_BACKFILL_PAGE_DELAY = 0.5


@asynccontextmanager
async def _dedicated_client():
    # fetch_with_retry adds the TMDB auth per request, so a bare client is enough.
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    ) as client:
        yield client


def _slice_oldest_ids(table: str, buckets: int) -> List[int]:
    """One night's slice: the oldest ceil(rows / buckets) ids of ``table`` (a
    trusted literal), never-refreshed rows first."""
    with get_connection() as conn:
        cursor = conn.cursor()
        total = cursor.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] or 0
        if total == 0:
            return []
        limit = -(-total // buckets)
        cursor.execute(
            f"SELECT tmdb_id FROM {table} ORDER BY last_updated ASC NULLS FIRST LIMIT %s",
            (limit,),
        )
        return [r["tmdb_id"] for r in cursor.fetchall()]


async def _refresh_ids(client, kind: str, ids: List[int]) -> int:
    """How many of ``ids`` were re-pulled and re-stored."""
    fetch = fetch_tmdb_show if kind == "show" else fetch_tmdb_movie
    done = 0
    for tid in ids:
        try:
            await fetch(client, tid, force_refresh=True)
            done += 1
        except Exception as e:
            logger.warning(f"Refresh failed for {kind} {tid}: {e}")
        await asyncio.sleep(_REFRESH_DELAY)
    return done


async def refresh_daily_slice(buckets: Optional[int] = None) -> Tuple[int, int]:
    """Re-pull one night's slice of each table; the re-store stamps last_updated,
    which moves those rows to the back of the queue."""
    buckets = buckets or get_settings().metadata_refresh_buckets

    show_ids = await asyncio.to_thread(_slice_oldest_ids, "tmdb_shows", buckets)
    movie_ids = await asyncio.to_thread(_slice_oldest_ids, "tmdb_movies", buckets)
    if not show_ids and not movie_ids:
        return (0, 0)

    async with _dedicated_client() as client:
        shows = await _refresh_ids(client, "show", show_ids)
        movies = await _refresh_ids(client, "movie", movie_ids)
    return (shows, movies)


async def _backfill_discover(client, kind: str, max_pages: int) -> int:
    """Page TMDB discover (``kind`` "tv" or "movie"), storing what the trending
    surfaces would have stored. TMDB caps discover at page 500."""
    url = f"https://api.themoviedb.org/3/discover/{kind}"
    persisted = 0
    for page in range(1, max_pages + 1):
        params = {**discover_params(kind), "page": page}
        data = await fetch_with_retry(client, url, params=params) or {}
        items = data.get("results") or []
        if not items:
            break
        persisted += len(await keep_non_anime(client, kind, items))

        total_pages = data.get("total_pages") or page
        if page >= min(total_pages, 500):
            break
        await asyncio.sleep(_BACKFILL_PAGE_DELAY)
    return persisted


async def backfill_catalogue(max_pages: Optional[int] = None) -> Tuple[int, int]:
    """(shows, movies) stored."""
    max_pages = max_pages or get_settings().metadata_backfill_pages
    async with _dedicated_client() as client:
        shows = await _backfill_discover(client, "tv", max_pages)
        movies = await _backfill_discover(client, "movie", max_pages)
    return (shows, movies)


# The job queue helpers are synchronous; callers run them through asyncio.to_thread.
_ACTIVE_STATUSES = ("requested", "running")


def job_status_payload(row: Optional[dict]) -> Optional[dict]:
    """A job row with the booleans the dashboard keys on; None before the first job."""
    if not row:
        return None
    st = row["status"]
    return {
        "state": st,
        "queued": st == "requested",
        "running": st == "running",
        "ok": True if st == "done" else (False if st == "failed" else None),
        "pages": row.get("pages"),
        "shows": row.get("shows"),
        "movies": row.get("movies"),
        "error": row.get("error"),
        "triggered_by": row.get("requested_by"),
        "requested_at": row.get("requested_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
    }


def request_backfill(pages: int, requested_by: str) -> Tuple[dict, bool]:
    """``(row, created)``. An active job is handed back with created=False instead
    of a new one, so a double-click or a second admin cannot stack runs."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO metadata_backfill_jobs (status, pages, requested_by)
            SELECT 'requested', %s, %s
            WHERE NOT EXISTS (SELECT 1 FROM metadata_backfill_jobs WHERE status = ANY(%s))
            RETURNING *
            """,
            (pages, requested_by, list(_ACTIVE_STATUSES)),
        )
        row = cursor.fetchone()
        if row:
            return dict(row), True
        cursor.execute(
            "SELECT * FROM metadata_backfill_jobs WHERE status = ANY(%s) ORDER BY requested_at DESC LIMIT 1",
            (list(_ACTIVE_STATUSES),),
        )
        return dict(cursor.fetchone()), False


def latest_backfill_job() -> Optional[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM metadata_backfill_jobs ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        return dict(row) if row else None


def _claim_backfill_job() -> Optional[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE metadata_backfill_jobs
            SET status='running', started_at=CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM metadata_backfill_jobs
                WHERE status='requested'
                ORDER BY requested_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def _finish_backfill_job(job_id: int, ok: bool, shows: Optional[int],
                         movies: Optional[int], error: Optional[str]) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE metadata_backfill_jobs
            SET status=%s, finished_at=CURRENT_TIMESTAMP, shows=%s, movies=%s, error=%s
            WHERE id=%s
            """,
            ("done" if ok else "failed", shows, movies, error, job_id),
        )


async def run_pending_backfill() -> Optional[Tuple[int, int]]:
    """Run the oldest queued backfill, if any; None when there was none."""
    row = await asyncio.to_thread(_claim_backfill_job)
    if not row:
        return None
    job_id = row["id"]
    pages = row.get("pages") or get_settings().metadata_backfill_pages
    logger.info(f"Draining backfill job #{job_id} ({pages} pages, by {row.get('requested_by')})")
    try:
        shows, movies = await backfill_catalogue(max_pages=pages)
        await asyncio.to_thread(_finish_backfill_job, job_id, True, shows, movies, None)
        logger.info(f"Backfill job #{job_id} done: {shows} shows, {movies} movies")
        return (shows, movies)
    except Exception as e:
        await asyncio.to_thread(_finish_backfill_job, job_id, False, None, None, str(e))
        logger.error(f"Backfill job #{job_id} failed: {e}")
        return None
