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
# How often the cache-worker polls the DB for newly-claimed (pending) downloads.
POLL_INTERVAL = max(2, int(os.getenv("CACHE_POLL_INTERVAL", "10")))  # seconds
# Don't start a download unless the target has at least this much headroom.
MIN_FREE_BYTES = int(os.getenv("CACHE_MIN_FREE_BYTES", str(2 * 1024 * 1024 * 1024)))  # 2 GiB

# Stream URL fragments that mean "don't cache this": our own cache output and the
# on-disk Local source — direct play (/local_proxy) AND its on-the-fly transcode
# (/local_hls) are both already a local file, so re-caching either is pointless/looping.
_SKIP_URL_FRAGMENTS = (fs.PROXY_PREFIX, "/local_proxy", "/local_hls")


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


def _is_loopback_proxy_url(url: str) -> bool:
    """True when ``url`` is one of the backend's OWN same-origin proxy routes — a
    ``/{source}_proxy`` path or the ``/player`` wrapper — i.e. a stream the cache
    can pull back over loopback (``INTERNAL_BASE``) reusing the per-source
    Referer/HMAC-signing/auth the live player relied on.

    This is the exact predicate ``_to_internal`` keys on, so "is this cacheable"
    and "can I rewrite it onto loopback" can never disagree.

    Anything else is NOT ours to pull and must never be cached:

      * a **raw third-party CDN link** — a stream the client resolved and delivered
        itself (extension / E3, or a direct-play file). Its token is bound to the
        VIEWER's IP/ASN, so a backend ffmpeg pull gets 403/429; and it's exactly the
        byte traffic the New System deliberately moved OFF the backend.
      * a **crimson-proxy edge link** (E2, ``CRIMSON_PROXY_BASE``, path ``/``) —
        pulling it re-imports the bandwidth we offloaded and rides the free edge's
        rate limit (429). This is the offloaded/relay case the old
        ``_is_external_proxy_url`` host check caught; the positive test subsumes it
        (an edge link's path is ``/``, so it fails here too) and additionally closes
        the raw-CDN gap that host check missed."""
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return "_proxy" in path or path.rstrip("/").endswith("player")


def _to_internal(url: str) -> str:
    """Rewrite an absolute backend URL onto the loopback INTERNAL_BASE so the
    download stays on-host. Leaves third-party URLs (direct-play streams) alone."""
    parsed = urlparse(url)
    # Only same-backend proxy/player paths are rewritten; a raw CDN URL (direct
    # play) is fetched as-is. (The cacheability gate already rejects those, so in
    # practice every job that reaches here is a loopback-proxy URL.)
    if _is_loopback_proxy_url(url):
        q = f"?{parsed.query}" if parsed.query else ""
        return f"{INTERNAL_BASE}{parsed.path}{q}"
    return url


class DownloadManager:
    def __init__(self) -> None:
        self._store = CacheStore()
        self._queue: Optional[asyncio.Queue] = None
        self._workers: list[asyncio.Task] = []
        self._poller: Optional[asyncio.Task] = None
        self._inflight: set[int] = set()  # entry ids currently queued/running (this process)
        self._started = False

    # ------------------------------------------------------------- lifecycle
    async def start_worker(self) -> None:
        """Start the out-of-process download worker: a small ffmpeg pool fed by a DB
        poll loop. ONLY the dedicated cache-worker service calls this
        (RUN_CACHE_WORKER); the api/api-sync replicas just mint tickets and claim
        rows (mint_ticket / maybe_enqueue), they never download. Idempotent."""
        if self._started:
            return
        self._started = True
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if not ffmpeg_available():
            logger.warning(
                "ffmpeg not found on PATH — video caching is inert (downloads will "
                "fail). Install ffmpeg in the image to enable caching."
            )
        # Requeue any download a previous worker was interrupted mid-remux on (deploy
        # that outran stop_grace_period, crash) — downloading -> pending, retried.
        try:
            n = await run_in_threadpool(self._store.reset_stale_jobs)
            if n:
                logger.info(f"Requeued {n} interrupted download(s) from a previous worker")
        except Exception as e:
            logger.error(f"Stale-job requeue failed: {e}")
        self._workers = [
            asyncio.create_task(self._worker(i)) for i in range(MAX_CONCURRENT)
        ]
        self._poller = asyncio.create_task(self._poll())
        logger.info(
            f"Cache download worker started ({MAX_CONCURRENT} ffmpeg slot(s), "
            f"polling every {POLL_INTERVAL}s)"
        )

    async def stop(self, drain_timeout: float = 110.0) -> None:
        """Graceful stop. Stop pulling new work first, then give in-flight remuxes a
        bounded chance to finish (Docker's stop_grace_period covers this window)
        before cancelling. Anything still running past the deadline is abandoned;
        its row stays ``downloading`` and the next worker's reset_stale_jobs requeues
        it. A no-op on the api/api-sync replicas (no poller, nothing in flight)."""
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None
        if self._inflight:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + drain_timeout
            logger.info(f"Draining {len(self._inflight)} in-flight cache download(s) before stop")
            while self._inflight and loop.time() < deadline:
                await asyncio.sleep(0.5)
        for w in self._workers:
            w.cancel()
        self._workers = []
        self._started = False

    # ------------------------------------------------------------------ gate
    async def _cacheable(self, stream: dict) -> bool:
        """Shared cacheability gate: caching is on, ffmpeg is present, and the stream
        is a media URL the backend can pull back over its OWN loopback proxy. Used
        both by the ticket stamp (watch path) and the row claim (confirm/warmup path)
        so the two never disagree on what's cacheable. Deliberately independent of
        the worker being started — the api replicas mint/claim without running the
        worker.

        The load-bearing check is ``_is_loopback_proxy_url``: the cache remuxes a
        stream by re-fetching it through the backend's own ``/{source}_proxy`` over
        loopback (``_to_internal``), which only works for our own same-origin proxy
        routes. Since the E2/E3 offload landed, ``/watch`` also surfaces streams the
        client delivers itself — raw CDN links (extension) and crimson-proxy edge
        links — and pulling those back through the backend is exactly what produced
        the 429 / wrong-ASN / edge-rate-limit cache failures. So caching is now
        gated *positively* on the same-origin proxy shape rather than blocklisting
        the two offload shapes we happened to know about."""
        if not await run_in_threadpool(self._store.get_enabled):
            return False
        url = (stream.get("url") or "")
        if any(frag in url for frag in _SKIP_URL_FRAGMENTS):
            return False  # our own cache output / the on-disk Local source
        media_url = _media_url_for_stream(stream)
        if not media_url:
            return False  # not a tappable media URL (e.g. a player-page iframe)
        if not _is_loopback_proxy_url(media_url):
            # Delivered by the client/edge, not fronted by our proxy — not ours to
            # pull. (Subsumes the old external-proxy-host guard; also catches raw
            # CDN links the client resolved locally.)
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
        media_type: str = "tv",
    ) -> Optional[str]:
        """If this stream is cacheable right now, return an opaque, signed ticket
        the player echoes back to ``/cache/confirm`` once the viewer has actually
        watched it — else None. Stamping (not downloading) here is what lets us
        cache the source the viewer *chose*, not whichever resolved fastest. Safe
        to call for every resolved stream; never raises into the watch path.

        ``media_type`` ("tv" | "movie") rides into the ticket so the confirm path
        keys the cache correctly. Movies and tv share one TMDB id space, so the
        cache key is namespaced by media_type (see CacheStore / fs.plan_rel_path) —
        a cached movie can't collide with a same-id show."""
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
                media_type=media_type,
            )
        except Exception as e:
            logger.error(f"mint_ticket failed: {e}")
            return None

    async def confirm_ticket(self, ticket_str: str) -> bool:
        """Player-confirmed watch: verify a ticket minted by :meth:`mint_ticket` and
        claim the download (write the pending DB row the cache-worker drains).
        Returns whether the ticket verified; the enabled/dedupe/space checks happen
        in :meth:`maybe_enqueue`. Never raises."""
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

    # --------------------------------------------------------------- enqueue
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
        """Claim a resolved stream for caching by writing (or reviving) the pending
        DB row the cache-worker drains. Safe to call for every stream — it
        self-filters, dedupes via the DB claim, and never raises into the watch/
        confirm path. The actual ffmpeg download happens out-of-process in the
        cache-worker service (see :meth:`start_worker` / :meth:`_poll`)."""
        try:
            if not await self._cacheable(stream):
                return

            target = await run_in_threadpool(fs.pick_write_target, MIN_FREE_BYTES)
            if not target:
                return

            # _cacheable() already vetted this is a tappable media URL (non-None).
            # Store the absolute (public-origin) URL; the worker rewrites it onto
            # loopback at download time via _to_internal.
            media_url = _media_url_for_stream(stream)
            language = (stream.get("language") or "").strip()
            source_origin = stream.get("source") or ""
            rel_path = fs.plan_rel_path(
                tmdb_id, season_number, episode_number, language, media_type=media_type
            )

            # Write/revive the pending row. The cache-worker's poll loop picks it up;
            # the unique constraint dedupes across replicas, so this is safe to call
            # from every api replica that handles the /cache/confirm.
            await run_in_threadpool(
                self._store.claim_download,
                tmdb_id=tmdb_id,
                media_type=media_type,
                season_number=season_number,
                episode_number=episode_number,
                anilist_id=anilist_id,
                language=language,
                source_origin=source_origin,
                target_id=target["id"],
                rel_path=rel_path,
                media_url=media_url or "",
            )
        except Exception as e:
            logger.error(f"maybe_enqueue failed: {e}")

    # ----------------------------------------------------------- poll (worker)
    async def _poll(self) -> None:
        """Producer: periodically drain newly-claimed (pending) rows from the DB into
        the ffmpeg slots. The DB row is the queue now — the api service's /cache/
        confirm writes pending rows, this worker downloads them — which is exactly
        what lets a download survive an api redeploy (the job lives in Postgres, not
        in the api process that handled the confirm)."""
        while True:
            try:
                await self._enqueue_pending()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"cache poll failed: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _enqueue_pending(self) -> None:
        assert self._queue is not None
        if not await run_in_threadpool(self._store.get_enabled):
            return
        # Only pull as many as we have free ffmpeg slots, so the in-process queue
        # stays tiny and pending work keeps surviving in the DB until a slot frees.
        available = MAX_CONCURRENT - len(self._inflight)
        if available <= 0:
            return
        rows = await run_in_threadpool(self._store.fetch_pending, available)
        for row in rows:
            entry_id = row["id"]
            if entry_id in self._inflight:
                continue
            job = self._row_to_job(row)
            if not job:
                await run_in_threadpool(
                    self._store.mark_failed, entry_id, "uncacheable row (no media_url)"
                )
                continue
            # Atomically claim pending -> downloading; lose the race (another slot or
            # replica grabbed it first) -> skip it.
            if not await run_in_threadpool(self._store.begin_download, entry_id):
                continue
            self._inflight.add(entry_id)
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                # We only ever fetch `available` <= free slots, so this is defensive.
                self._inflight.discard(entry_id)
                await run_in_threadpool(self._store.mark_failed, entry_id, "cache queue full")
                return

    @staticmethod
    def _row_to_job(row: dict) -> Optional[dict]:
        """Build the in-process download job from a pending DB row, or None when the
        row can't be turned into a fetchable URL (missing media_url)."""
        media_url = (row.get("media_url") or "").strip()
        if not media_url:
            return None
        language = row.get("language") or ""
        tmdb_id, sn, en = row["tmdb_id"], row["season_number"], row["episode_number"]
        media_type = row.get("media_type") or "tv"
        if media_type == "movie":
            label = f"movie-tmdb-{tmdb_id}"
        else:
            label = f"tmdb-{tmdb_id} S{sn}E{en}"
        return {
            "entry_id": row["id"],
            "media_url": _to_internal(media_url),
            "abs_path": os.path.join(row["target_path"], row["rel_path"]),
            "source_origin": row.get("source_origin") or "",
            "language": language,
            "label": label + (f" [{language}]" if language else ""),
        }

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

        # Row is already 'downloading' (begin_download claimed it in the poll loop).
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

        Pure stream copy first (``-c copy``, no re-encode — fast and cheap). A
        second pass that re-encodes ONLY the audio to AAC is attempted *solely*
        when the first failed because the source's audio codec has no mp4 tag
        (AC-3 / E-AC-3 / MP2 — what some dub tracks ship). Other failures (network,
        missing segments) aren't retried; a second full pull would just fail again.
        Video (the expensive, bulky stream) is never re-encoded either way."""
        rc, tail = await self._ffmpeg_attempt(media_url, out_path, reencode_audio=False)
        if rc == 0 or not _is_audio_tag_error(tail):
            return rc, tail
        logger.info(f"[cache] copy-mux failed on audio codec (rc={rc}); retrying with audio re-encode")
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
            # HLS hardening reversal. Our segment URLs are proxied as
            # ``/voe_proxy?u=<encoded .ts>`` — the real extension lives in the query
            # string, so the path looks extension-less. ffmpeg 7.1+ added a strict
            # ``extension_picky`` check that rejects such segments ("is not in
            # allowed_segment_extensions") even with ``-allowed_extensions ALL``,
            # yielding a zero-stream output. Turn the picky check off; keep
            # allowed_extensions ALL for the playlist/key files (disguised .html,
            # AES-128 keys fetched over http through our proxy).
            "-extension_picky", "0",
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


# Stderr signatures of an mp4 muxer rejecting an audio codec it can't tag (the
# only failure worth retrying with the audio re-encoded — see ``_run_ffmpeg``).
_AUDIO_TAG_ERROR_HINTS = (
    "could not find tag for codec",
    "codec not currently supported in container",
)


def _is_audio_tag_error(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(h in s for h in _AUDIO_TAG_ERROR_HINTS)


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
