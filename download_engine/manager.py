"""The aria2-backed download worker.

The ``download_jobs`` row is the queue. Only the download-worker service
(RUN_DOWNLOAD_WORKER) submits rows to aria2 and polls them; run exactly one, since
nothing stops two from submitting the same row. Each poll submits pending rows
into free slots, then reconciles every active row with aria2: progress, a magnet's
metadata-to-data gid hand-off, completion (move out of staging) or failure.

Pause, resume and cancel go straight to aria2 from whichever replica handled the
admin request, so they apply without waiting for a poll.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from . import aria2, fs
from .aria2 import Aria2Error
from .db import STATUS_ACTIVE, STATUS_PAUSED, store
from core.config import get_settings

logger = logging.getLogger("download_engine.manager")


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def pause_job(job: dict) -> Optional[dict]:
    if job.get("gid"):
        await aria2.pause(job["gid"])
    return await asyncio.to_thread(store.set_status, job["id"], STATUS_PAUSED)


async def resume_job(job: dict) -> Optional[dict]:
    """Unpause in aria2 when it still knows the gid, otherwise requeue."""
    if job.get("gid"):
        try:
            await aria2.unpause(job["gid"])
            return await asyncio.to_thread(store.set_status, job["id"], STATUS_ACTIVE)
        except Aria2Error:
            pass
    return await asyncio.to_thread(store.requeue, job["id"])


async def cancel_job(job: dict) -> None:
    """The caller deletes the row."""
    if job.get("gid"):
        await aria2.remove(job["gid"])
    if job.get("staging_dir"):
        await asyncio.to_thread(fs.cleanup_staging, job["staging_dir"])


class DownloadManager:
    def __init__(self) -> None:
        self.store = store
        self._poller: Optional[asyncio.Task] = None
        self._started = False
        self._warned_no_space = False

    async def start_worker(self) -> None:
        """Only the download-worker service calls this. Idempotent."""
        if self._started:
            return
        self._started = True
        if not await aria2.is_available():
            logger.warning(
                f"aria2 sidecar not reachable at {get_settings().aria2_rpc_url}: downloads stay "
                "pending until it is. Check the aria2 service and ARIA2_RPC_SECRET."
            )
        # Active rows are not reset here, unlike the cache worker's: aria2 runs in
        # its own container and keeps downloading across a worker roll, so a reset
        # would make it write the file twice. The monitor requeues only the gids
        # aria2 has forgotten, which resume from the staging control file.
        self._poller = asyncio.create_task(self._poll())
        settings = get_settings()
        logger.info(
            f"Download worker started (max {settings.download_max_active} active, "
            f"polling every {settings.download_poll_interval}s)"
        )

    async def stop(self) -> None:
        """Running downloads carry on in aria2 and the next worker re-attaches."""
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None
        self._started = False

    def worker_stats(self) -> dict:
        """Job counts by status for /metrics. They come from the table, so every
        replica reports the same cluster-wide numbers. Blocking: one grouped COUNT."""
        by_status = {}
        try:
            by_status = {k: v for k, v in self.store.stats().items() if k != "total_bytes"}
        except Exception as e:
            logger.debug(f"download worker stats unavailable: {e}")
        return {"running": self._started, "by_status": by_status}

    async def _poll(self) -> None:
        while True:
            try:
                await self._submit_pending()
                await self._monitor_active()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"download poll failed: {e}")
            await asyncio.sleep(get_settings().download_poll_interval)

    async def _submit_pending(self) -> None:
        free = get_settings().download_max_active - await asyncio.to_thread(self.store.count_active)
        if free <= 0:
            return
        rows = await asyncio.to_thread(self.store.fetch_pending, free)
        for row in rows:
            try:
                await self._submit_one(row)
            except Exception as e:
                logger.error(f"submit failed for job {row['id']}: {e}")
                await asyncio.to_thread(self.store.mark_failed, row["id"], str(e))

    async def _submit_one(self, row: dict) -> None:
        target = await asyncio.to_thread(fs.pick_write_target, get_settings().download_min_free_bytes)
        if not target:
            # The row stays pending for the next poll; warn once, not every poll.
            if not self._warned_no_space:
                logger.warning(
                    "No download-enabled local source has enough free space: "
                    f"holding downloads (need {get_settings().download_min_free_bytes // (1024*1024)} MiB free)"
                )
                self._warned_no_space = True
            return
        self._warned_no_space = False

        staging_dir = fs.plan_staging_dir(target["downloads_dir"], row["id"])
        await asyncio.to_thread(os.makedirs, staging_dir, exist_ok=True)
        gid = await aria2.add_uri(row["source_url"], staging_dir)
        await asyncio.to_thread(
            self.store.mark_active,
            row["id"],
            gid=gid,
            target_source_id=target["id"],
            target_path=target["path"],
            dest_dir=target["downloads_dir"],
            staging_dir=staging_dir,
        )
        logger.info(
            f"[download] started job {row['id']} ({row['kind']}) "
            f"-> {target['label']!r} ({staging_dir})"
        )

    async def _monitor_active(self) -> None:
        rows = await asyncio.to_thread(self.store.fetch_active)
        for row in rows:
            gid = row.get("gid")
            if not gid:
                continue
            try:
                status = await aria2.tell_status(gid)
            except Aria2Error:
                # aria2 restarted; the resubmit resumes from the staging control file.
                logger.info(f"[download] job {row['id']} gid {gid} unknown to aria2; requeuing")
                await asyncio.to_thread(self.store.requeue, row["id"])
                continue
            await self._apply_status(row, status)

    async def _apply_status(self, row: dict, status: dict) -> None:
        state = status.get("status")
        done = _int(status.get("completedLength"))
        total = _int(status.get("totalLength")) or None
        speed = _int(status.get("downloadSpeed"))

        # A magnet's metadata download "completes" and hands off to a new gid.
        if state == "complete":
            followed = aria2.followed_gid(status)
            if followed:
                logger.info(f"[download] job {row['id']} metadata resolved; following gid {followed}")
                await asyncio.to_thread(self.store.update_gid, row["id"], followed)
                return
            await self._finalize(row, done)
            return

        if state == "error":
            msg = status.get("errorMessage") or f"aria2 error code {status.get('errorCode')}"
            await asyncio.to_thread(self.store.mark_failed, row["id"], msg)
            logger.warning(f"[download] job {row['id']} failed: {msg}")
            return

        if state == "removed":
            await asyncio.to_thread(self.store.mark_failed, row["id"], "cancelled in aria2")
            return

        await asyncio.to_thread(self.store.update_progress, row["id"], done, total, speed)

    async def _finalize(self, row: dict, done_bytes: int) -> None:
        """Publish the payload, then drop aria2's result row so the gid does not linger."""
        try:
            final_path = await asyncio.to_thread(
                fs.publish, row["staging_dir"], row["dest_dir"], row.get("name")
            )
        except Exception as e:
            logger.error(f"[download] publish failed for job {row['id']}: {e}")
            await asyncio.to_thread(self.store.mark_failed, row["id"], f"publish failed: {e}")
            return
        if row.get("gid"):
            await aria2.remove(row["gid"])
        await asyncio.to_thread(self.store.mark_complete, row["id"], final_path, done_bytes)
        logger.info(
            f"[download] job {row['id']} complete ({done_bytes / 1_048_576:.1f} MiB) -> {final_path}"
        )


manager = DownloadManager()
