"""
Background download manager (aria2-backed).

The DB row is the queue: an admin route on any replica writes a ``pending``
``download_jobs`` row; only the dedicated **download-worker** (RUN_DOWNLOAD_WORKER)
submits it to the aria2 sidecar and polls it to completion. So a download survives
an api redeploy, and the ``begin_submit`` claim stops two workers double-adding a
job.

The worker runs one poll loop:

  1. **submit** — for each free slot, claim a ``pending`` row, pick the first
     download-enabled source root with free space, and ``aria2.addUri`` it into a
     per-job staging dir (``crimson-downloads/.incoming/<id>``). Record the gid.
  2. **monitor** — for each ``active`` row, ``aria2.tellStatus``: update
     bytes/speed; follow a magnet's metadata->data gid hand-off; on completion move
     the payload out of staging into ``crimson-downloads`` (leech-only) and mark it
     ``complete``; on error mark it ``failed``. If aria2 has forgotten the gid (it
     was restarted), requeue the row — aria2 resumes from the control file left in
     staging.

Pause / resume / cancel are issued straight against aria2 from the admin route
(the sidecar is reachable from every replica over the internal network), so they
take effect immediately without waiting for the worker's poll.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from starlette.concurrency import run_in_threadpool

from . import aria2, fs
from .aria2 import Aria2Error
from .db import (
    DownloadStore,
    KIND_TORRENT,
    STATUS_ACTIVE,
    STATUS_PAUSED,
)

logger = logging.getLogger("download_engine.manager")

# Tunables.
MAX_ACTIVE = max(1, int(os.getenv("DOWNLOAD_MAX_ACTIVE", "3")))
POLL_INTERVAL = max(2, int(os.getenv("DOWNLOAD_POLL_INTERVAL", "5")))  # seconds
# Don't start a download unless the chosen root has at least this much headroom.
MIN_FREE_BYTES = int(os.getenv("DOWNLOAD_MIN_FREE_BYTES", str(2 * 1024 * 1024 * 1024)))  # 2 GiB


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- route-facing controls (any replica; aria2 is reachable over the network) ---
async def pause_job(store: DownloadStore, job: dict) -> Optional[dict]:
    """Pause a job. If it has already started, pause it in aria2 too."""
    if job.get("gid"):
        await aria2.pause(job["gid"])
    return await run_in_threadpool(store.set_status, job["id"], STATUS_PAUSED)


async def resume_job(store: DownloadStore, job: dict) -> Optional[dict]:
    """Resume a paused job. If aria2 still holds its gid, unpause in place; otherwise
    re-queue it for the worker to (re-)submit."""
    if job.get("gid"):
        try:
            await aria2.unpause(job["gid"])
            return await run_in_threadpool(store.set_status, job["id"], STATUS_ACTIVE)
        except Aria2Error:
            pass
    return await run_in_threadpool(store.requeue, job["id"])


async def cancel_and_delete_job(store: DownloadStore, job: dict) -> None:
    """Remove a job from aria2 (if running) and clean its staging dir. The row itself
    is deleted by the caller (the route already did the DB delete to get ``job``)."""
    if job.get("gid"):
        await aria2.remove(job["gid"])
    if job.get("staging_dir"):
        await run_in_threadpool(fs.cleanup_staging, job["staging_dir"])


class DownloadManager:
    def __init__(self) -> None:
        self._store = DownloadStore()
        self._poller: Optional[asyncio.Task] = None
        self._started = False
        self._warned_no_space = False

    # ------------------------------------------------------------- lifecycle
    async def start_worker(self) -> None:
        """Start the download poll loop. ONLY the dedicated download-worker service
        calls this (RUN_DOWNLOAD_WORKER); other replicas just write pending rows and
        issue pause/resume/cancel. Idempotent."""
        if self._started:
            return
        self._started = True
        if not await aria2.is_available():
            logger.warning(
                f"aria2 sidecar not reachable at {aria2.RPC_URL} — downloads will stay "
                "pending until it is. Check the aria2 service + ARIA2_RPC_SECRET."
            )
        # NB: unlike the cache worker (which IS the ffmpeg process), aria2 runs in a
        # SEPARATE container that keeps downloading across a worker roll. So we do NOT
        # reset 'active' rows on startup — that would re-submit a still-running
        # download and make aria2 write it twice. Instead the monitor loop reconciles:
        # for each active row it polls aria2, and only requeues rows whose gid aria2
        # has genuinely forgotten (i.e. aria2 itself restarted), which then resume from
        # the staging control file. So a worker-only roll seamlessly re-attaches.
        self._poller = asyncio.create_task(self._poll())
        logger.info(
            f"Download worker started (max {MAX_ACTIVE} active, polling every {POLL_INTERVAL}s)"
        )

    async def stop(self) -> None:
        """Stop the poll loop. In-flight aria2 downloads keep running in the sidecar
        and are reclaimed (resumed) by the next worker start — nothing is lost. A no-op
        on replicas that never started the worker."""
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None
        self._started = False

    # ---------------------------------------------------------------- loop
    async def _poll(self) -> None:
        while True:
            try:
                await self._submit_pending()
                await self._monitor_active()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"download poll failed: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _submit_pending(self) -> None:
        free = MAX_ACTIVE - await run_in_threadpool(self._store.count_active)
        if free <= 0:
            return
        rows = await run_in_threadpool(self._store.fetch_pending, free)
        for row in rows:
            # Claim the row so no other worker submits it (single-worker today, but the
            # claim keeps a misconfigured second worker safe).
            if not await run_in_threadpool(self._store.begin_submit, row["id"]):
                continue
            try:
                await self._submit_one(row)
            except Exception as e:
                logger.error(f"submit failed for job {row['id']}: {e}")
                await run_in_threadpool(self._store.mark_failed, row["id"], str(e))

    async def _submit_one(self, row: dict) -> None:
        target = await run_in_threadpool(fs.pick_write_target, MIN_FREE_BYTES)
        if not target:
            # No download-enabled root with room right now — leave the row pending and
            # retry next tick. Warn once so the log isn't spammed every poll.
            if not self._warned_no_space:
                logger.warning(
                    "No download-enabled local source has enough free space — "
                    f"holding downloads (need {MIN_FREE_BYTES // (1024*1024)} MiB free)"
                )
                self._warned_no_space = True
            return
        self._warned_no_space = False

        staging_dir = fs.plan_staging_dir(target["downloads_dir"], row["id"])
        await run_in_threadpool(os.makedirs, staging_dir, exist_ok=True)
        is_torrent = row["kind"] == KIND_TORRENT
        gid = await aria2.add_uri(row["source_url"], staging_dir, seed=False)
        await run_in_threadpool(
            self._store.mark_active,
            row["id"],
            gid=gid,
            target_source_id=target["id"],
            target_path=target["path"],
            dest_dir=target["downloads_dir"],
            staging_dir=staging_dir,
        )
        logger.info(
            f"[download] started job {row['id']} ({'torrent' if is_torrent else 'http'}) "
            f"-> {target['label']!r} ({staging_dir})"
        )

    async def _monitor_active(self) -> None:
        rows = await run_in_threadpool(self._store.fetch_active)
        for row in rows:
            gid = row.get("gid")
            if not gid:
                continue
            try:
                status = await aria2.tell_status(gid)
            except Aria2Error:
                # aria2 forgot this gid (restart) — requeue; it resumes from the
                # staging control file on the next submit.
                logger.info(f"[download] job {row['id']} gid {gid} unknown to aria2; requeuing")
                await run_in_threadpool(self._store.requeue, row["id"])
                continue
            await self._apply_status(row, status)

    async def _apply_status(self, row: dict, status: dict) -> None:
        state = status.get("status")
        done = _int(status.get("completedLength"))
        total = _int(status.get("totalLength")) or None
        speed = _int(status.get("downloadSpeed"))

        # Magnet metadata->data hand-off: the metadata download 'completes' with a new
        # gid in followedBy. Track that gid instead of finalizing.
        if state == "complete":
            followed = aria2.followed_gid(status)
            if followed:
                logger.info(f"[download] job {row['id']} metadata resolved; following gid {followed}")
                await run_in_threadpool(self._store.update_gid, row["id"], followed)
                return
            await self._finalize(row, done)
            return

        if state == "error":
            msg = status.get("errorMessage") or f"aria2 error code {status.get('errorCode')}"
            await run_in_threadpool(self._store.mark_failed, row["id"], msg)
            logger.warning(f"[download] job {row['id']} failed: {msg}")
            return

        if state == "removed":
            await run_in_threadpool(self._store.mark_failed, row["id"], "cancelled in aria2")
            return

        # active / waiting / paused: just record progress.
        await run_in_threadpool(self._store.update_progress, row["id"], done, total, speed)

    async def _finalize(self, row: dict, done_bytes: int) -> None:
        """Move the finished payload out of staging into crimson-downloads and mark the
        row complete. Drops the aria2 result row so the gid doesn't linger."""
        try:
            final_path = await run_in_threadpool(
                fs.publish, row["staging_dir"], row["dest_dir"], row.get("name")
            )
        except Exception as e:
            logger.error(f"[download] publish failed for job {row['id']}: {e}")
            await run_in_threadpool(self._store.mark_failed, row["id"], f"publish failed: {e}")
            return
        if row.get("gid"):
            await aria2.remove(row["gid"])
        await run_in_threadpool(self._store.mark_complete, row["id"], final_path, done_bytes)
        logger.info(
            f"[download] job {row['id']} complete ({done_bytes / 1_048_576:.1f} MiB) -> {final_path}"
        )


# Process-wide singleton wired up in api.py's lifespan.
manager = DownloadManager()
