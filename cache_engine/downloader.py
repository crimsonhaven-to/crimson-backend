"""
Background download manager for the server-side video cache.

When ``/watch`` resolves a playable stream and caching is enabled, it calls
``manager.maybe_enqueue(...)``. The manager:

  1. decides whether the stream is cacheable (a real hls/mp4 stream we can pull;
     never the cache's own ``/cache_proxy`` output, the on-disk ``/local_proxy``
     source, or a player-page iframe we can't tap),
  2. atomically reserves the slot in the DB (``CacheStore.claim_download`` —
     cross-replica dedup via the shared unique constraint), and
  3. enqueues the job for a small pool of workers.

A worker pulls the stream through **the backend's own proxy over loopback** and
hands the URL to **ffmpeg**, which follows the HLS playlist (or copies the mp4)
and remuxes it — no re-encode (``-c copy``) — into a single ``.mp4`` on the NAS.
Going through our own proxy means the per-source Referer/signing/auth that the
live player relies on is reused verbatim; ffmpeg never talks to the upstream CDN
directly. On success the row flips to ``ready`` and the ``CacheScraper`` will
surface it on the next play.

Single-process state (the queue) — but the DB claim is the source of truth, so
across Swarm replicas exactly one node downloads any given episode.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Optional
from urllib.parse import parse_qs, urlparse

from starlette.concurrency import run_in_threadpool

from .db import CacheStore
from . import fs, ticket

logger = logging.getLogger("cache_engine.downloader")

# Internal base the downloader uses to reach our own proxy routes. Loopback by
# default (uvicorn binds 0.0.0.0:8000) so segment traffic never leaves the host
# or hits the public reverse proxy / login wall. Override if uvicorn binds
# elsewhere.
INTERNAL_BASE = os.getenv("CACHE_INTERNAL_BASE", "http://127.0.0.1:8000").rstrip("/")

# Tunables.
MAX_CONCURRENT = max(1, int(os.getenv("CACHE_MAX_CONCURRENT", "1")))
DOWNLOAD_TIMEOUT = int(os.getenv("CACHE_DOWNLOAD_TIMEOUT", "3600"))  # seconds, per episode
QUEUE_MAX = int(os.getenv("CACHE_QUEUE_MAX", "200"))
# Don't start a download unless the target has at least this much headroom.
MIN_FREE_BYTES = int(os.getenv("CACHE_MIN_FREE_BYTES", str(2 * 1024 * 1024 * 1024)))  # 2 GiB

# Stream URL fragments that mean "don't cache this": our own cache output and the
# on-disk Local source (already a file) — re-caching either is pointless/looping.
_SKIP_URL_FRAGMENTS = (fs.PROXY_PREFIX, "/local_proxy")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _media_url_for_stream(stream: dict) -> Optional[str]:
    """The absolute media URL to feed ffmpeg, or None when the stream isn't a
    tappable file.

    * hls / mp4  -> the stream URL itself.
    * iframe     -> only our backend ``/player?src=<path>`` wrapper, whose ``src``
                    is the real same-origin stream path; anything else (e.g. the
                    Movish player-page proxy) has no clean stream to pull.
    """
    url = (stream.get("url") or "").strip()
    if not url:
        return None
    stype = (stream.get("type") or "").lower()

    if stype in ("hls", "mp4"):
        return url

    if stype == "iframe":
        parsed = urlparse(url)
        if parsed.path.endswith("/player") or parsed.path == "/player":
            src = (parse_qs(parsed.query).get("src") or [None])[0]
            if src and src.startswith("/"):
                # src is a same-origin path; resolve it against the same origin.
                return f"{parsed.scheme}://{parsed.netloc}{src}"
    return None


def _to_internal(url: str) -> str:
    """Rewrite an absolute backend URL onto the loopback INTERNAL_BASE so the
    download stays on-host. Leaves third-party URLs (direct-play streams) alone."""
    parsed = urlparse(url)
    # Only same-backend proxy/player paths are rewritten; a raw CDN URL (direct
    # play) is fetched as-is.
    if parsed.path.startswith(("/", "")) and (
        "_proxy" in parsed.path or parsed.path.rstrip("/").endswith("player")
    ):
        q = f"?{parsed.query}" if parsed.query else ""
        return f"{INTERNAL_BASE}{parsed.path}{q}"
    return url


class DownloadManager:
    def __init__(self) -> None:
        self._store = CacheStore()
        self._queue: Optional[asyncio.Queue] = None
        self._workers: list[asyncio.Task] = []
        self._inflight: set[int] = set()  # entry ids currently queued/running (this process)
        self._started = False

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if not ffmpeg_available():
            logger.warning(
                "ffmpeg not found on PATH — video caching is inert (downloads will "
                "fail). Install ffmpeg in the image to enable caching."
            )
        # Recover from a previous process that died mid-download.
        try:
            n = await run_in_threadpool(self._store.reset_stale_jobs)
            if n:
                logger.info(f"Reset {n} stale cache job(s) from a previous run")
        except Exception as e:
            logger.error(f"Stale-job reset failed: {e}")
        self._workers = [
            asyncio.create_task(self._worker(i)) for i in range(MAX_CONCURRENT)
        ]
        logger.info(f"Cache download manager started ({MAX_CONCURRENT} worker(s))")

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers = []
        self._started = False

    # ------------------------------------------------------------------ gate
    async def _cacheable(self, stream: dict) -> bool:
        """Shared cacheability gate: caching is on, ffmpeg is present, the stream
        is a tappable media URL, and it isn't our own cache/local output. Used both
        by the ticket stamp (watch path) and the real enqueue (confirm path) so the
        two never disagree on what's cacheable."""
        if not self._started or self._queue is None:
            return False
        if not await run_in_threadpool(self._store.get_enabled):
            return False
        url = (stream.get("url") or "")
        if any(frag in url for frag in _SKIP_URL_FRAGMENTS):
            return False  # our own cache output / the on-disk Local source
        if not _media_url_for_stream(stream):
            return False
        if not ffmpeg_available():
            return False
        return True

    # ----------------------------------------------------------- watch ticket
    async def mint_ticket(
        self,
        stream: dict,
        *,
        tmdb_id: int,
        season_number: int,
        episode_number: int,
        anilist_id: Optional[int],
    ) -> Optional[str]:
        """If this stream is cacheable right now, return an opaque, signed ticket
        the player echoes back to ``/cache/confirm`` once the viewer has actually
        watched it — else None. Stamping (not downloading) here is what lets us
        cache the source the viewer *chose*, not whichever resolved fastest. Safe
        to call for every resolved stream; never raises into the watch path."""
        try:
            if not await self._cacheable(stream):
                return None
            return ticket.mint(
                url=(stream.get("url") or ""),
                type=(stream.get("type") or ""),
                source=(stream.get("source") or ""),
                language=(stream.get("language") or ""),
                tmdb_id=tmdb_id,
                season_number=season_number,
                episode_number=episode_number,
                anilist_id=anilist_id,
            )
        except Exception as e:
            logger.error(f"mint_ticket failed: {e}")
            return None

    async def confirm_ticket(self, ticket_str: str) -> bool:
        """Player-confirmed watch: verify a ticket minted by :meth:`mint_ticket`
        and enqueue the real download. Returns whether the ticket verified; the
        enabled/dedupe/space checks happen in :meth:`maybe_enqueue`. Never raises."""
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
            )
            return True
        except Exception as e:
            logger.error(f"confirm_ticket failed: {e}")
            return False

    # --------------------------------------------------------------- enqueue
    async def maybe_enqueue(
        self,
        stream: dict,
        *,
        tmdb_id: int,
        season_number: int,
        episode_number: int,
        anilist_id: Optional[int],
    ) -> None:
        """Consider a resolved stream for caching. Safe to call for every stream —
        it self-filters and dedupes, and never raises into the watch path."""
        try:
            if not await self._cacheable(stream):
                return

            target = await run_in_threadpool(fs.pick_write_target, MIN_FREE_BYTES)
            if not target:
                return

            # _cacheable() already vetted this is a tappable media URL (non-None).
            media_url = _media_url_for_stream(stream)
            language = (stream.get("language") or "").strip()
            source_origin = stream.get("source") or ""
            rel_path = fs.plan_rel_path(tmdb_id, season_number, episode_number, language)

            row = await run_in_threadpool(
                self._store.claim_download,
                tmdb_id=tmdb_id,
                season_number=season_number,
                episode_number=episode_number,
                anilist_id=anilist_id,
                language=language,
                source_origin=source_origin,
                target_id=target["id"],
                rel_path=rel_path,
            )
            if not row:
                return  # already pending/downloading/ready (here or another replica)

            entry_id = row["id"]
            if entry_id in self._inflight:
                return
            self._inflight.add(entry_id)
            job = {
                "entry_id": entry_id,
                "media_url": _to_internal(media_url),
                "abs_path": os.path.join(target["path"], rel_path),
                "source_origin": source_origin,
                "language": language,
                "label": f"tmdb-{tmdb_id} S{season_number}E{episode_number}"
                + (f" [{language}]" if language else ""),
            }
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                self._inflight.discard(entry_id)
                await run_in_threadpool(self._store.mark_failed, entry_id, "cache queue full")
                logger.warning(f"Cache queue full; dropped {job['label']}")
        except Exception as e:
            logger.error(f"maybe_enqueue failed: {e}")

    # ---------------------------------------------------------------- worker
    async def _worker(self, idx: int) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            entry_id = job["entry_id"]
            try:
                await self._download(job)
            except asyncio.CancelledError:
                # Shutdown: leave the row pending/downloading; reset_stale_jobs
                # will reclaim it next start.
                raise
            except Exception as e:
                logger.error(f"Cache download crashed for {job['label']}: {e}")
                try:
                    await run_in_threadpool(self._store.mark_failed, entry_id, str(e))
                except Exception:
                    pass
            finally:
                self._inflight.discard(entry_id)
                self._queue.task_done()

    async def _download(self, job: dict) -> None:
        entry_id = job["entry_id"]
        abs_path = job["abs_path"]
        part_path = abs_path + ".part"

        await run_in_threadpool(self._store.mark_downloading, entry_id)
        await run_in_threadpool(os.makedirs, os.path.dirname(abs_path), exist_ok=True)
        # Clean any leftover partial from a previous failed attempt.
        await run_in_threadpool(_unlink_quiet, part_path)

        logger.info(f"[cache] downloading {job['label']} from {job['source_origin']!r}")
        rc, stderr_tail = await self._run_ffmpeg(job["media_url"], part_path)

        if rc != 0:
            await run_in_threadpool(_unlink_quiet, part_path)
            msg = f"ffmpeg exit {rc}: {stderr_tail}" if stderr_tail else f"ffmpeg exit {rc}"
            await run_in_threadpool(self._store.mark_failed, entry_id, msg)
            logger.warning(f"[cache] failed {job['label']}: {msg}")
            return

        size = await run_in_threadpool(_size_or_zero, part_path)
        if size <= 0:
            await run_in_threadpool(_unlink_quiet, part_path)
            await run_in_threadpool(self._store.mark_failed, entry_id, "empty output")
            logger.warning(f"[cache] failed {job['label']}: empty output")
            return

        await run_in_threadpool(os.replace, part_path, abs_path)  # atomic publish
        await run_in_threadpool(self._store.mark_ready, entry_id, size)
        logger.info(f"[cache] ready {job['label']} ({size / 1_048_576:.1f} MiB) -> {abs_path}")

    async def _run_ffmpeg(self, media_url: str, out_path: str) -> tuple[int, str]:
        """Remux ``media_url`` into ``out_path`` (mp4). Returns (returncode,
        stderr-tail).

        Two tiers: first a pure stream copy (``-c copy``, no re-encode — the fast,
        cheap path that works for the typical AAC-audio source). If that fails to
        mux — overwhelmingly because the source's audio codec has no mp4 tag, i.e.
        AC-3 / E-AC-3 / MP2 tracks that German/other **dub** sources ship while the
        Japanese **sub** versions (AAC) don't — we retry copying the *video* but
        transcoding only the *audio* to AAC. Video (the expensive, bulky stream) is
        never re-encoded."""
        rc, tail = await self._ffmpeg_attempt(media_url, out_path, reencode_audio=False)
        if rc == 0:
            return rc, tail
        # The mp4 muxer rejecting an un-taggable audio codec surfaces as
        # AVERROR(EINVAL) (exit 234) / "Error opening output files". Re-encoding
        # just the audio to AAC is the cheap, reliable fix; only worth a second
        # pass, not a probe, since the copy path covers the common case.
        logger.info(f"[cache] copy-mux failed (rc={rc}); retrying with audio re-encode")
        return await self._ffmpeg_attempt(media_url, out_path, reencode_audio=True)

    async def _ffmpeg_attempt(
        self, media_url: str, out_path: str, *, reencode_audio: bool
    ) -> tuple[int, str]:
        codec_args = (
            ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
            if reencode_audio
            else ["-c", "copy"]
        )
        args = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel", "error",
            # HLS: segments may be served with disguised extensions (.html) and
            # AES-128 keys are fetched over http through our proxy.
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-i", media_url,
            *codec_args,
            "-movflags", "+faststart",
            "-f", "mp4",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=DOWNLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, f"timed out after {DOWNLOAD_TIMEOUT}s"
        # Keep the last few stderr lines, not just one: the actionable mp4 error
        # ("Could not find tag for codec …") prints above the generic trailing
        # "Error opening output files" line.
        tail = (err or b"").decode("utf-8", errors="replace").strip().splitlines()
        return proc.returncode, " | ".join(tail[-4:]) if tail else ""


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


# Process-wide singleton wired up in api.py's lifespan.
manager = DownloadManager()
