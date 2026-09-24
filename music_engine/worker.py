"""The music-worker loop: turns ``pending`` tracks into tagged files on the share,
and keeps synced playlists current.

Only the music-worker service runs it (RUN_MUSIC_WORKER). The api replicas only
write rows, so an import survives an api redeploy and a download survives an
import. Per track::

    match (unless a url is known)  ->  fetch  ->  tag with Spotify's metadata
    ->  rename into Artist/Album/  ->  ready

The provider is the only part that talks to the outside world, and it does so
slowly on purpose: two slots, claimed only on a poll tick, keep one home IP from
looking like a scraper.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from core.config import get_settings
from core.http_client import http_client

from . import fs, library, tagging
from .db import STATUS_UNMATCHED, store
from .provider import MusicProvider, ProviderError, TrackQuery, get_provider

logger = logging.getLogger("crimson.music.worker")

SLOTS = 2
POLL_SECONDS = 10
SYNC_CHECK_SECONDS = 300
# After the provider reports a rate limit or a network failure, for every slot.
BACKOFF_SECONDS = 120.0


class MusicWorker:
    def __init__(self) -> None:
        self._jobs: dict[int, asyncio.Task] = {}
        self._poller: Optional[asyncio.Task] = None
        self._paused_until = 0.0
        self._last_sync_check = 0.0
        # Playlists whose M3U is stale, rewritten once the queue goes quiet.
        self._dirty: set[int] = set()

    async def start(self) -> None:
        if self._poller is not None:
            return
        if not get_settings().music_root:
            logger.info("MUSIC_ROOT not set, the music worker stays off")
            return
        if not fs.available():
            logger.warning("MUSIC_ROOT %s is missing or not writable", fs.root())
        if get_provider() is None:
            logger.warning("no music provider in this build: playlists sync, nothing downloads")
        requeued = await asyncio.to_thread(store.reset_stale)
        if requeued:
            logger.info("requeued %d interrupted track(s)", requeued)
        self._poller = asyncio.create_task(self._poll())
        logger.info("music worker started (%d slots)", SLOTS)

    async def stop(self, drain_timeout: float = 100.0) -> None:
        """Stop taking work and let running tracks finish inside the service's
        stop grace. A cancelled track stays ``working`` and is requeued on start."""
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + drain_timeout
        while self._jobs and loop.time() < deadline:
            await asyncio.sleep(0.5)
        for task in list(self._jobs.values()):
            task.cancel()

    async def _poll(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                if loop.time() - self._last_sync_check >= SYNC_CHECK_SECONDS:
                    self._last_sync_check = loop.time()
                    synced = await library.sync_due()
                    if synced:
                        logger.info("synced %d playlist(s)", synced)
                started = await self._start_pending(loop.time())
                if not started and not self._jobs and self._dirty:
                    await self._write_dirty_playlists()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("music poll failed: %s", e)
            await asyncio.sleep(POLL_SECONDS)

    async def _start_pending(self, now: float) -> int:
        provider = get_provider()
        free = SLOTS - len(self._jobs)
        if provider is None or free <= 0 or now < self._paused_until or not fs.available():
            return 0
        started = 0
        for row in await asyncio.to_thread(store.fetch_pending, free):
            if row["id"] in self._jobs or not await asyncio.to_thread(store.claim, row["id"]):
                continue
            self._jobs[row["id"]] = asyncio.create_task(self._run(provider, row))
            started += 1
        return started

    async def _run(self, provider: MusicProvider, track: dict) -> None:
        track_id = track["id"]
        label = f"{', '.join(track['artists'])} - {track['title']}"
        try:
            await self._process(provider, track, label)
        except asyncio.CancelledError:
            raise
        except ProviderError as e:
            if e.retryable:
                self._paused_until = asyncio.get_running_loop().time() + BACKOFF_SECONDS
                await asyncio.to_thread(store.requeue, track_id, str(e))
            else:
                await asyncio.to_thread(store.mark_failed, track_id, str(e))
            logger.warning("[music] %s: %s", label, e)
        except Exception as e:
            logger.error("[music] %s crashed: %s", label, e)
            await asyncio.to_thread(store.requeue, track_id, str(e))
        finally:
            await asyncio.to_thread(fs.remove_work_dir, track_id)
            self._jobs.pop(track_id, None)

    async def _process(self, provider: MusicProvider, track: dict, label: str) -> None:
        track_id = track["id"]

        url = track.get("match_url")
        if not url:
            match = await provider.match(
                TrackQuery(
                    title=track["title"],
                    artists=track["artists"],
                    album=track["album"],
                    duration_ms=track["duration_ms"],
                    isrc=track.get("isrc"),
                )
            )
            if match.kind == "review":
                await asyncio.to_thread(store.mark_review, track_id, match.candidates)
                logger.info("[music] %s needs review (%d candidates)", label, len(match.candidates))
                return
            if match.kind == "none" or not match.url:
                await asyncio.to_thread(
                    store.mark_failed,
                    track_id,
                    "Nothing looked like this recording. Search for it by hand.",
                    status=STATUS_UNMATCHED,
                )
                logger.info("[music] %s unmatched", label)
                return
            url = match.url
            await asyncio.to_thread(store.set_matched, track_id, url, match.candidates)

        work = await asyncio.to_thread(fs.work_dir, track_id)
        fetched = await provider.fetch(url, work)
        cover = await _download_cover(track.get("cover_url"), work)
        album = track["album"] or fetched.album

        rel_path = await asyncio.to_thread(fs.unique_rel_path, fs.plan_rel_path({**track, "album": album}))
        tagged = os.path.join(work, "tagged.m4a")
        error = await tagging.write_tagged(fetched.path, cover, tagged, track, album)
        if error:
            await asyncio.to_thread(store.mark_failed, track_id, error)
            logger.warning("[music] %s: %s", label, error)
            return

        size = os.path.getsize(tagged)
        await asyncio.to_thread(fs.publish, tagged, rel_path)
        cover_path = await asyncio.to_thread(fs.place_cover, cover, rel_path) if cover else None
        await asyncio.to_thread(
            store.mark_ready,
            track_id,
            rel_path=rel_path,
            cover_path=cover_path,
            file_size=size,
            album=album,
        )
        for playlist in await asyncio.to_thread(store.playlists_for_track, track_id):
            self._dirty.add(playlist["id"])
        logger.info("[music] ready %s -> %s", label, rel_path)

    async def _write_dirty_playlists(self) -> None:
        dirty, self._dirty = self._dirty, set()
        for playlist_id in dirty:
            playlist = await asyncio.to_thread(store.get_playlist_by_id, playlist_id)
            if playlist:
                await asyncio.to_thread(library.write_playlist_file, playlist)


async def _download_cover(url: Optional[str], work: str) -> Optional[str]:
    """A track without its cover is still a track, so any failure here is None."""
    if not url:
        return None
    try:
        async with http_client() as client:
            response = await client.get(url)
        if response.status_code != 200 or not response.content:
            return None
        path = os.path.join(work, "cover.jpg")
        await asyncio.to_thread(_write_bytes, path, response.content)
        return path
    except Exception as e:
        logger.info("cover download failed for %s: %s", url, e)
        return None


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


worker = MusicWorker()
