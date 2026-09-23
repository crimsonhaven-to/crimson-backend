"""Remuxing watched streams onto the NAS.

The api replicas only mint tickets and, once the player confirms a watch, write a
``pending`` row. The cache-worker service (RUN_CACHE_WORKER) polls those rows and
runs one ffmpeg per row, pulling the stream back through the backend's own proxy
over loopback so the per-source Referer, signing and auth are reused verbatim and
ffmpeg never talks to an upstream CDN. The stream is copied (``-c copy``) into a
single mp4; the row flips to ``ready`` and the Cache source surfaces it on the
next play.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional
from urllib.parse import parse_qs, urlparse

from core import ffmpeg
from core.config import get_settings
from core.ffmpeg import ffmpeg_available

from . import fs, ticket
from .db import store

logger = logging.getLogger("cache_engine.downloader")

# Streams that are already a file on this host: the cache's own output and the
# Local source (direct play and its transcode). Re-caching them would loop.
_SKIP_URL_FRAGMENTS = (fs.PROXY_PREFIX, "/local_proxy", "/local_hls")

# The mp4 muxer's complaints about an audio codec it cannot tag (AC-3, E-AC-3,
# MP2 dub tracks). Only these are worth a second pass with the audio re-encoded;
# a network or missing-segment failure would just fail again.
_AUDIO_TAG_ERROR_HINTS = (
    "could not find tag for codec",
    "codec not currently supported in container",
)


def _media_url_for_stream(stream: dict) -> Optional[str]:
    """The URL to feed ffmpeg: the stream itself for hls/mp4, or the same-origin
    ``src`` of our ``/player`` iframe wrapper. Any other iframe is a player page
    with no clean stream to pull."""
    url = (stream.get("url") or "").strip()
    if not url:
        return None
    stype = (stream.get("type") or "").lower()
    if stype in ("hls", "mp4"):
        return url
    if stype == "iframe":
        parsed = urlparse(url)
        if parsed.path.endswith("/player"):
            srcs = parse_qs(parsed.query).get("src")
            if srcs and srcs[0].startswith("/"):
                return f"{parsed.scheme}://{parsed.netloc}{srcs[0]}"
    return None


def _is_loopback_proxy_url(url: str) -> bool:
    """Whether ``url`` is one of our own ``/{source}_proxy`` routes or the
    ``/player`` wrapper, the only shapes the worker can pull back over loopback.

    The gate is positive on purpose. A raw CDN link the client resolved itself is
    bound to the viewer's IP, so a backend pull gets 403/429, and a crimson-proxy
    edge link (path ``/``) would drag offloaded bandwidth back onto the backend and
    hit the edge's rate limit."""
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return "_proxy" in path or path.rstrip("/").endswith("player")


def _to_internal(url: str) -> str:
    """Point a stored public URL at ``cache_internal_base`` so the pull stays on
    the host. Only loopback-proxy URLs pass the gate, so every row has one."""
    parsed = urlparse(url)
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{get_settings().cache_internal_base}{parsed.path}{query}"


def _row_to_job(row: dict) -> Optional[dict]:
    media_url = (row.get("media_url") or "").strip()
    if not media_url:
        return None
    language = row.get("language") or ""
    if row.get("media_type") == "movie":
        label = f"movie-tmdb-{row['tmdb_id']}"
    else:
        label = f"tmdb-{row['tmdb_id']} S{row['season_number']}E{row['episode_number']}"
    return {
        "entry_id": row["id"],
        "media_url": _to_internal(media_url),
        "abs_path": os.path.join(row["target_path"], row["rel_path"]),
        "source_origin": row.get("source_origin") or "",
        "label": label + (f" [{language}]" if language else ""),
    }


def _is_audio_tag_error(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(h in s for h in _AUDIO_TAG_ERROR_HINTS)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class DownloadManager:
    def __init__(self) -> None:
        self.store = store
        self._jobs: dict[int, asyncio.Task] = {}
        self._poller: Optional[asyncio.Task] = None
        self._started = False

    async def start_worker(self) -> None:
        """Only the cache-worker service calls this. Idempotent."""
        if self._started:
            return
        self._started = True
        if not ffmpeg_available():
            logger.warning("ffmpeg not found on PATH: every cache download will fail")
        try:
            n = await asyncio.to_thread(self.store.reset_stale_jobs)
            if n:
                logger.info(f"Requeued {n} interrupted download(s) from a previous worker")
        except Exception as e:
            logger.error(f"Stale-job requeue failed: {e}")
        self._poller = asyncio.create_task(self._poll())
        settings = get_settings()
        logger.info(
            f"Cache download worker started ({settings.cache_max_concurrent} ffmpeg slot(s), "
            f"polling every {settings.cache_poll_interval}s)"
        )

    async def stop(self, drain_timeout: float = 110.0) -> None:
        """Stop taking work, then give running remuxes until ``drain_timeout``
        (inside Docker's stop_grace_period) before cancelling them. A cancelled
        row stays ``downloading`` and the next worker's reset_stale_jobs requeues it."""
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None
        if self._jobs:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + drain_timeout
            logger.info(f"Draining {len(self._jobs)} in-flight cache download(s) before stop")
            while self._jobs and loop.time() < deadline:
                await asyncio.sleep(0.5)
        for task in list(self._jobs.values()):
            task.cancel()
        self._started = False

    def worker_stats(self) -> dict:
        """In-process only, so safe to call from a /metrics scrape. All zeros on an
        api replica, which never runs the worker."""
        return {
            "running": self._started,
            "inflight": len(self._jobs),
            "slots": get_settings().cache_max_concurrent,
        }

    async def cacheable(self, stream: dict) -> bool:
        """The one gate for both minting a ticket and claiming a row, so the two
        never disagree. Independent of the worker: api replicas run it too."""
        if not await asyncio.to_thread(self.store.get_enabled):
            return False
        url = stream.get("url") or ""
        if any(frag in url for frag in _SKIP_URL_FRAGMENTS):
            return False
        media_url = _media_url_for_stream(stream)
        if not media_url or not _is_loopback_proxy_url(media_url):
            return False
        return ffmpeg_available()

    async def mint_ticket(
        self,
        stream: dict,
        *,
        tmdb_id: int,
        season_number: int,
        episode_number: int,
        anilist_id: Optional[int],
        media_type: str = "tv",
    ) -> Optional[str]:
        """A ticket for the player to redeem once the viewer has really watched,
        or None when the stream is not cacheable. Never raises into /watch."""
        try:
            if not await self.cacheable(stream):
                return None
            return ticket.mint(
                url=stream.get("url") or "",
                type=stream.get("type") or "",
                source=stream.get("source") or "",
                language=stream.get("language") or "",
                tmdb_id=tmdb_id,
                season_number=season_number,
                episode_number=episode_number,
                anilist_id=anilist_id,
                media_type=media_type,
            )
        except Exception as e:
            logger.error(f"mint_ticket failed: {e}")
            return None

    async def confirm_ticket(self, ticket_str: str) -> bool:
        """Whether the ticket verified. The claim itself is best effort."""
        try:
            data = ticket.verify(ticket_str)
            if not data:
                return False
            await self.maybe_enqueue(
                {
                    "url": data["url"],
                    "type": data["type"],
                    "source": data["source"],
                    "language": data["language"],
                },
                tmdb_id=data["tmdb_id"],
                season_number=data["season_number"],
                episode_number=data["episode_number"],
                anilist_id=data["anilist_id"],
                media_type=data.get("media_type") or "tv",
            )
            return True
        except Exception as e:
            logger.error(f"confirm_ticket failed: {e}")
            return False

    async def maybe_enqueue(
        self,
        stream: dict,
        *,
        tmdb_id: int,
        season_number: int,
        episode_number: int,
        anilist_id: Optional[int],
        media_type: str = "tv",
    ) -> None:
        """Write (or revive) the ``pending`` row for this stream. Safe to call from
        any replica for any stream: it filters itself, the unique index dedupes,
        and it never raises."""
        try:
            if not await self.cacheable(stream):
                return
            target = await asyncio.to_thread(
                fs.pick_write_target, get_settings().cache_min_free_bytes
            )
            if not target:
                return
            language = (stream.get("language") or "").strip()
            # The public URL is stored; the worker rewrites it onto loopback.
            await asyncio.to_thread(
                self.store.claim_download,
                tmdb_id=tmdb_id,
                media_type=media_type,
                season_number=season_number,
                episode_number=episode_number,
                anilist_id=anilist_id,
                language=language,
                source_origin=stream.get("source") or "",
                target_id=target["id"],
                rel_path=fs.plan_rel_path(
                    tmdb_id, season_number, episode_number, language, media_type=media_type
                ),
                media_url=_media_url_for_stream(stream) or "",
            )
        except Exception as e:
            logger.error(f"maybe_enqueue failed: {e}")

    async def _poll(self) -> None:
        while True:
            try:
                await self._start_pending()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"cache poll failed: {e}")
            await asyncio.sleep(get_settings().cache_poll_interval)

    async def _start_pending(self) -> None:
        if not await asyncio.to_thread(self.store.get_enabled):
            return
        # Fetch no more rows than there are free slots, so waiting work stays in
        # Postgres where a restart cannot lose it.
        free = get_settings().cache_max_concurrent - len(self._jobs)
        if free <= 0:
            return
        for row in await asyncio.to_thread(self.store.fetch_pending, free):
            entry_id = row["id"]
            if entry_id in self._jobs:
                continue
            job = _row_to_job(row)
            if not job:
                await asyncio.to_thread(
                    self.store.mark_failed, entry_id, "uncacheable row (no media_url)"
                )
                continue
            if not await asyncio.to_thread(self.store.begin_download, entry_id):
                continue
            self._jobs[entry_id] = asyncio.create_task(self._run_job(job))

    async def _run_job(self, job: dict) -> None:
        entry_id = job["entry_id"]
        try:
            await self._download(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Cache download crashed for {job['label']}: {e}")
            try:
                await asyncio.to_thread(self.store.mark_failed, entry_id, str(e))
            except Exception:
                pass
        finally:
            self._jobs.pop(entry_id, None)

    async def _download(self, job: dict) -> None:
        entry_id = job["entry_id"]
        abs_path = job["abs_path"]
        part_path = abs_path + ".part"

        await asyncio.to_thread(os.makedirs, os.path.dirname(abs_path), exist_ok=True)
        await asyncio.to_thread(_unlink_quiet, part_path)

        logger.info(f"[cache] downloading {job['label']} from {job['source_origin']!r}")
        rc, stderr_tail = await self._run_ffmpeg(job["media_url"], part_path)

        if rc != 0:
            await asyncio.to_thread(_unlink_quiet, part_path)
            msg = f"ffmpeg exit {rc}: {stderr_tail}" if stderr_tail else f"ffmpeg exit {rc}"
            await asyncio.to_thread(self.store.mark_failed, entry_id, msg)
            logger.warning(f"[cache] failed {job['label']}: {msg}")
            return

        size = await asyncio.to_thread(_size_or_zero, part_path)
        if size <= 0:
            await asyncio.to_thread(_unlink_quiet, part_path)
            await asyncio.to_thread(self.store.mark_failed, entry_id, "empty output")
            logger.warning(f"[cache] failed {job['label']}: empty output")
            return

        await asyncio.to_thread(os.replace, part_path, abs_path)
        await asyncio.to_thread(self.store.mark_ready, entry_id, size)
        logger.info(f"[cache] ready {job['label']} ({size / 1_048_576:.1f} MiB) -> {abs_path}")

    async def _run_ffmpeg(self, media_url: str, out_path: str) -> tuple[int, str]:
        """``(returncode, stderr_tail)``. Video is never re-encoded; audio only on
        the retry after an untaggable audio codec."""
        rc, tail = await self._ffmpeg_attempt(media_url, out_path, reencode_audio=False)
        if rc == 0 or not _is_audio_tag_error(tail):
            return rc, tail
        logger.info(f"[cache] copy-mux failed on audio codec (rc={rc}); retrying with audio re-encode")
        return await self._ffmpeg_attempt(media_url, out_path, reencode_audio=True)

    async def _ffmpeg_attempt(
        self, media_url: str, out_path: str, *, reencode_audio: bool
    ) -> tuple[int, str]:
        codec_args = (
            ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"] if reencode_audio else ["-c", "copy"]
        )
        args = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel", "error",
            # Proxied segment URLs look extension-less (``/voe_proxy?u=<.ts>``), and
            # ffmpeg 7.1+ rejects those even with ``-allowed_extensions ALL``,
            # producing a zero-stream output. ALL stays for disguised playlists and
            # AES keys fetched through the proxy.
            "-extension_picky", "0",
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-i", media_url,
            *codec_args,
            "-movflags", "+faststart",
            "-f", "mp4",
            out_path,
        ]
        timeout = get_settings().cache_download_timeout
        rc, _out, lines = await ffmpeg.run(args, timeout=timeout)
        if rc is None:
            return 124, f"timed out after {timeout}s"
        # The actionable mp4 error prints above the generic trailing
        # "Error opening output files" line, so keep a few lines.
        return rc, " | ".join(lines[-4:])


manager = DownloadManager()
