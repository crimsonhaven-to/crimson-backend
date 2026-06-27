"""
Background maintenance for the non-anime metadata tables (tmdb_shows / tmdb_movies).

These tables are written lazily — on overview open (fetch_tmdb_show / fetch_tmdb_movie)
and from search/trending discovery (_persist_discovered_*). That leaves two gaps this
module fills. Both the work and the queue here are driven exclusively from the single
RUN_DB_SYNC replica (the api-sync container), so exactly one container ever churns this
much metadata:

* refresh_daily_slice — there's no upstream to signal a TMDB change (unlike the Fribb
  dataset), so the catalogue is swept in slices: each night the oldest 1/N of each table
  is re-pulled, cycling the whole table over N nights, then repeating.
* backfill_catalogue — page TMDB discover to pre-populate the tables beyond what's been
  browsed. Triggered from the Admin dashboard (queued in the DB, drained here) or once at
  startup via RUN_METADATA_BACKFILL.

The Admin "Start Backfill" button runs on a portless-api-sync-unreachable serving replica,
so it enqueues a metadata_backfill_jobs row; ``run_pending_backfill`` (polled by api-sync)
claims and runs it. The job-queue helpers below own that table.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Tuple

import httpx

from core.config import Config
from core.db_pool import get_connection
from core.http_client import fetch_with_retry
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

# Pacing between individual TMDB calls. Gentle on TMDB's rate limit and, for the
# backfill, on WAL churn / standby replication during the bulk insert.
_REFRESH_DELAY = 0.25   # seconds between per-row refresh fetches
_BACKFILL_PAGE_DELAY = 0.5  # seconds between discover pages


@asynccontextmanager
async def _dedicated_client():
    """A short-lived, loop-local httpx client for the maintenance jobs.

    The nightly refresh + backfill run in the scheduler's worker thread via
    ``asyncio.run()`` (a fresh event loop each tick), so they must NOT borrow the
    process-wide shared AsyncClient (which is bound to the main event loop). TMDB
    auth is applied per-request by ``fetch_with_retry``, so a bare client behaves
    identically to the shared one — it just doesn't reuse the warm connection pool,
    which is fine for these background sweeps."""
    async with httpx.AsyncClient(
        timeout=Config.REQUEST_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    ) as client:
        yield client


# --- NIGHTLY STALENESS REFRESH (1/N slice) ---------------------------------
def _slice_oldest_ids(table: str, buckets: int) -> List[int]:
    """tmdb_ids of the oldest ceil(rowcount / buckets) rows in ``table``.

    This is one night's slice: the stalest 1/N of the table (rows never refreshed
    sort first via NULLS FIRST). ``table`` is a trusted literal (never user input).
    """
    if buckets < 1:
        buckets = 1
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
    """Re-pull each id from TMDB (force-refresh so the row is re-upserted), paced.
    ``kind`` is 'show' or 'movie'. Best-effort per id. Returns how many succeeded."""
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


async def refresh_daily_slice(buckets: int = None) -> Tuple[int, int]:
    """Refresh one night's slice (oldest 1/buckets) of each metadata table.

    Re-pulling re-upserts the row (stamping last_updated), so over ``buckets`` nights
    the whole catalogue is swept back into agreement with TMDB. Returns
    (shows_refreshed, movies_refreshed)."""
    buckets = buckets if buckets is not None else Config.METADATA_REFRESH_BUCKETS

    loop = asyncio.get_event_loop()
    show_ids = await loop.run_in_executor(None, _slice_oldest_ids, "tmdb_shows", buckets)
    movie_ids = await loop.run_in_executor(None, _slice_oldest_ids, "tmdb_movies", buckets)
    if not show_ids and not movie_ids:
        return (0, 0)

    async with _dedicated_client() as client:
        shows = await _refresh_ids(client, "show", show_ids)
        movies = await _refresh_ids(client, "movie", movie_ids)
    return (shows, movies)


# --- CATALOGUE BACKFILL -----------------------------------------------------
async def _backfill_discover(client, kind: str, genre_map: dict, max_pages: int) -> int:
    """Page TMDB discover/{kind} and persist each (non-anime, postered) result.

    Mirrors the filtering of fetch_trending_shows / fetch_trending_movies so the
    backfilled rows match what the surfaces would themselves have cached. Stops at
    the real total_pages or ``max_pages`` (TMDB caps discover at page 500)."""
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


async def backfill_catalogue(max_pages: int = None) -> Tuple[int, int]:
    """One-shot pre-population of tmdb_shows / tmdb_movies from TMDB discover.

    Paced between pages. Returns (shows, movies) persisted."""
    max_pages = max_pages if max_pages is not None else Config.METADATA_BACKFILL_PAGES
    async with _dedicated_client() as client:
        tv_genre_map = await fetch_tmdb_genre_map(client, "tv")
        movie_genre_map = await fetch_tmdb_genre_map(client, "movie")
        shows = await _backfill_discover(client, "tv", tv_genre_map, max_pages)
        movies = await _backfill_discover(client, "movie", movie_genre_map, max_pages)
    return (shows, movies)


# --- BACKFILL JOB QUEUE (metadata_backfill_jobs) ---------------------------
# The admin button runs on a serving replica; api-sync drains the queue. Helpers
# are sync (DB) — callers wrap them in run_in_executor / run_in_threadpool.
_ACTIVE = ("requested", "running")


def job_status_payload(row: Optional[dict]) -> Optional[dict]:
    """Shape a metadata_backfill_jobs row for the Admin dashboard (and derive the
    running/queued/ok booleans the frontend keys on). ``None`` when no job yet."""
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
    """Enqueue a backfill request. Returns (row, created). If a job is already
    requested/running, no new row is inserted and the existing one is returned with
    created=False — so a double-click or a second admin can't stack runs."""
    with get_connection() as conn:
        cursor = conn.cursor()
        # Atomic: insert only when nothing is active, returning the new row.
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
        # Something is already active — hand it back instead.
        cursor.execute(
            "SELECT * FROM metadata_backfill_jobs WHERE status IN ('requested', 'running') "
            "ORDER BY requested_at DESC LIMIT 1"
        )
        return dict(cursor.fetchone()), False


def latest_backfill_job() -> Optional[dict]:
    """The most recent job row (any status), for the status endpoint."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM metadata_backfill_jobs ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        return dict(row) if row else None


def _claim_backfill_job() -> Optional[dict]:
    """Atomically claim the oldest still-'requested' job (mark it 'running').
    Returns the claimed row, or None if the queue is empty."""
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
    """Claim and run one queued backfill, if any. Called on a short interval by the
    RUN_DB_SYNC replica. Returns (shows, movies) when it ran one, else None."""
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(None, _claim_backfill_job)
    if not row:
        return None
    job_id = row["id"]
    pages = row.get("pages") or Config.METADATA_BACKFILL_PAGES
    logger.info(f"Draining backfill job #{job_id} ({pages} pages, by {row.get('requested_by')})")
    try:
        shows, movies = await backfill_catalogue(max_pages=pages)
        await loop.run_in_executor(None, _finish_backfill_job, job_id, True, shows, movies, None)
        logger.info(f"Backfill job #{job_id} done: {shows} shows, {movies} movies")
        return (shows, movies)
    except Exception as e:
        await loop.run_in_executor(None, _finish_backfill_job, job_id, False, None, None, str(e))
        logger.error(f"Backfill job #{job_id} failed: {e}")
        return None
