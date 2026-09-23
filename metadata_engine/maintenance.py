"""
Background maintenance for the non-anime metadata tables.

Those tables are written lazily, on overview open and from search and trending
discovery, which leaves two gaps this module fills. Both run only on the single
RUN_DB_SYNC replica, so exactly one container churns this much metadata:

* refresh_daily_slice, because nothing upstream signals a TMDB change the way the
  Fribb dataset does. Each night the oldest 1/N of each table is re-pulled,
  cycling the whole table over N nights.
* backfill_catalogue, which pages TMDB discover to pre-populate the tables beyond
  what has been browsed.

The admin's backfill button runs on a serving replica that cannot reach the
portless api-sync container, so it enqueues a row that ``run_pending_backfill``
claims. The job-queue helpers below own that table.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple

import httpx

from core.config import get_settings
from core.db_pool import get_connection
from core.http_client import REQUEST_TIMEOUT, fetch_with_retry
from metadata_engine.store import (
    get_first_anilist_ids,
    _persist_discovered_show,
    _persist_discovered_movie,
)
from metadata_engine.tmdb import (
    fetch_tmdb_show,
    fetch_tmdb_movie,
    fetch_tmdb_genre_map,
    _looks_like_anime,
    _looks_like_anime_movie,
)

logger = logging.getLogger("crimson.metadata.maintenance")

# Gentle on TMDB's rate limit and, during a bulk backfill, on standby replication.
_REFRESH_DELAY = 0.25       # between per-row refresh fetches
_BACKFILL_PAGE_DELAY = 0.5  # between discover pages


@asynccontextmanager
async def _dedicated_client():
    """A short-lived, loop-local httpx client for the maintenance jobs.

    These run in the scheduler's worker thread on a fresh event loop each tick, so
    they must not borrow the shared AsyncClient, which is bound to the main loop.
    ``fetch_with_retry`` applies TMDB auth per request, so a bare client behaves
    identically; it just misses the warm pool, which is fine for a background
    sweep."""
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    ) as client:
        yield client


# --- NIGHTLY STALENESS REFRESH (1/N slice) ---------------------------------
def _slice_oldest_ids(table: str, buckets: int) -> List[int]:
    """The oldest ceil(rowcount / buckets) tmdb_ids in ``table``: one night's
    slice. Rows never refreshed sort first. ``table`` is a trusted literal.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        total = cursor.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] or 0
        if total == 0:
            return []
        limit = -(-total // buckets)  # ceil division
        cursor.execute(
            f"SELECT tmdb_id FROM {table} ORDER BY last_updated ASC NULLS FIRST LIMIT %s",
            (limit,),
        )
        return [r["tmdb_id"] for r in cursor.fetchall()]


async def _refresh_ids(client, kind: str, ids: List[int]) -> int:
    """Re-pull each id from TMDB, forcing the row to be re-upserted. Paced, and
    best-effort per id. Returns how many succeeded."""
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
    """Refresh one night's slice of each metadata table.

    Re-pulling stamps last_updated, so over ``buckets`` nights the whole catalogue
    is swept back into agreement with TMDB."""
    buckets = buckets or get_settings().metadata_refresh_buckets

    show_ids = await asyncio.to_thread(_slice_oldest_ids, "tmdb_shows", buckets)
    movie_ids = await asyncio.to_thread(_slice_oldest_ids, "tmdb_movies", buckets)
    if not show_ids and not movie_ids:
        return (0, 0)

    async with _dedicated_client() as client:
        shows = await _refresh_ids(client, "show", show_ids)
        movies = await _refresh_ids(client, "movie", movie_ids)
    return (shows, movies)


# --- CATALOGUE BACKFILL -----------------------------------------------------
async def _backfill_discover(client, kind: str, genre_map: dict, max_pages: int) -> int:
    """Page TMDB discover and persist each non-anime, postered result.

    Mirrors the trending fetchers' filtering, so backfilled rows match what those
    surfaces would have cached themselves. Stops at the real total_pages or
    ``max_pages``; TMDB caps discover at page 500."""
    url = f"https://api.themoviedb.org/3/discover/{kind}"
    persisted = 0
    for page in range(1, max_pages + 1):
        params = {
            "page": page,
            "include_adult": "false",
            "language": "en-US",
            "without_genres": "16",          # exclude Animation (keeps anime out)
            "sort_by": "popularity.desc",
            "vote_count.gte": 200 if kind == "tv" else 300,  # quality floor
        }
        data = await fetch_with_retry(client, url, params=params)
        items = (data or {}).get("results") or []
        if not items:
            break

        if kind == "tv":
            anilist_by_tmdb = get_first_anilist_ids([it["id"] for it in items if it.get("id")])
            for item in items:
                tid = item.get("id")
                if not tid or anilist_by_tmdb.get(tid) or _looks_like_anime(item):
                    continue
                if not item.get("poster_path"):
                    continue
                _persist_discovered_show(item, genre_map)
                persisted += 1
        else:
            for item in items:
                if not item.get("id") or _looks_like_anime_movie(item):
                    continue
                if not item.get("poster_path"):
                    continue
                _persist_discovered_movie(item, genre_map)
                persisted += 1

        total_pages = data.get("total_pages") or page
        if page >= min(total_pages, 500):
            break
        await asyncio.sleep(_BACKFILL_PAGE_DELAY)
    return persisted


async def backfill_catalogue(max_pages: Optional[int] = None) -> Tuple[int, int]:
    """One-shot pre-population of both tables from TMDB discover, paced between
    pages. Returns how many of each were persisted."""
    max_pages = max_pages or get_settings().metadata_backfill_pages
    async with _dedicated_client() as client:
        tv_genre_map = await fetch_tmdb_genre_map(client, "tv")
        movie_genre_map = await fetch_tmdb_genre_map(client, "movie")
        shows = await _backfill_discover(client, "tv", tv_genre_map, max_pages)
        movies = await _backfill_discover(client, "movie", movie_genre_map, max_pages)
    return (shows, movies)


# --- BACKFILL JOB QUEUE ------------------------------------------------------
# The admin button runs on a serving replica and api-sync drains the queue. These
# helpers are synchronous, so callers wrap them in a threadpool.
_ACTIVE = ("requested", "running")


def job_status_payload(row: Optional[dict]) -> Optional[dict]:
    """Shape a job row for the dashboard, deriving the booleans the frontend keys
    on. ``None`` when there is no job yet."""
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
    """Enqueue a backfill request, returning (row, created). An already active job
    is returned with created=False rather than inserted again, so a double-click or
    a second admin cannot stack runs."""
    with get_connection() as conn:
        cursor = conn.cursor()
        # Atomic: inserts only when nothing is active.
        cursor.execute(
            """
            INSERT INTO metadata_backfill_jobs (status, pages, requested_by)
            SELECT 'requested', %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM metadata_backfill_jobs WHERE status IN ('requested', 'running')
            )
            RETURNING *
            """,
            (pages, requested_by),
        )
        row = cursor.fetchone()
        if row:
            return dict(row), True
        # Something is already active, so hand that back.
        cursor.execute(
            "SELECT * FROM metadata_backfill_jobs WHERE status IN ('requested', 'running') "
            "ORDER BY requested_at DESC LIMIT 1"
        )
        return dict(cursor.fetchone()), False


def latest_backfill_job() -> Optional[dict]:
    """The most recent job row of any status, for the status endpoint."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM metadata_backfill_jobs ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        return dict(row) if row else None


def _claim_backfill_job() -> Optional[dict]:
    """Atomically claim the oldest 'requested' job, or None when the queue is
    empty."""
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
    """Claim and run one queued backfill, if any. Polled on a short interval by the
    sync replica. None when there was nothing to run."""
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
