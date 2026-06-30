"""
On-the-fly HLS transcoding for the "Local" source.

Direct-play (``/local_proxy``) handles browser-native containers; this module
handles everything else (mkv/avi/ts/…) when a source has **encoding** enabled.
It exposes a *stateless* VOD HLS surface:

  * a media playlist computed purely from the file's duration — N fixed-length
    segments — so the player gets a fully seekable timeline up front, and
  * per-segment transcodes: each ``seg{n}.ts`` request runs one short ffmpeg that
    fast-seeks to ``n * SEGMENT_SECONDS``, transcodes just that window to MPEG-TS
    (H.264 + AAC) and pipes it back.

Stateless = no per-viewer session dir, no cleanup, no cross-replica affinity: any
api replica can serve any segment, and seeking is just "jump to segment k". The
trade-off is one ffmpeg spawn per segment and a possible micro-glitch at segment
boundaries — acceptable for a self-hosted Jellyfin-lite, and the price of not
maintaining live transcode sessions.

``-output_ts_offset`` places each segment's timestamps at its real position on the
timeline so hls.js stitches them seamlessly, and ``-force_key_frames`` opens every
segment on a keyframe so it decodes independently.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
import subprocess
from typing import Optional, Tuple

logger = logging.getLogger("local_engine.transcode")

# Segment length (seconds). 6s is the HLS convention — long enough to amortise the
# per-segment ffmpeg spawn, short enough to keep seeks snappy.
SEGMENT_SECONDS = max(2, int(os.getenv("LOCAL_HLS_SEGMENT_SECONDS", "6")))

# x264 knobs. ``veryfast`` keeps a single 1080p transcode within ~1 core (the api
# container's budget); raise quality with a slower preset / lower CRF if you have
# the headroom.
_PRESET = os.getenv("LOCAL_HLS_PRESET", "veryfast")
_CRF = os.getenv("LOCAL_HLS_CRF", "21")
_AUDIO_BITRATE = os.getenv("LOCAL_HLS_AUDIO_BITRATE", "160k")

# Hard ceiling on a single segment transcode so a pathological file can't pin a
# worker forever. A 6s segment that needs >120s to encode is broken, not slow.
_SEGMENT_TIMEOUT = int(os.getenv("LOCAL_HLS_SEGMENT_TIMEOUT", "120"))

# Bounded (path, mtime, size) -> duration cache so the playlist (and every segment
# request, which re-validates the count) doesn't re-probe the file each time.
_DURATION_CACHE: dict[tuple, float] = {}
_DURATION_CACHE_MAX = 4096


def tools_available() -> bool:
    """True only when BOTH ffmpeg and ffprobe are on PATH — encoding needs ffprobe
    (duration) as well as ffmpeg. The admin dashboard greys the toggle out when
    this is False so an operator isn't promised a feature the image can't deliver."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _cache_key(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (path, int(st.st_mtime), st.st_size)


def probe_duration(path: str) -> Optional[float]:
    """Media duration in seconds via ffprobe, memoised by (path, mtime, size).
    Returns None if the file can't be probed (missing/corrupt/no duration). Blocking
    — call from a route via ``run_in_threadpool``."""
    key = _cache_key(path)
    if key is None:
        return None
    cached = _DURATION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float((out.stdout or "").strip())
    except (subprocess.SubprocessError, ValueError) as e:
        logger.warning(f"ffprobe failed for {path!r}: {e}")
        return None
    if duration <= 0 or not math.isfinite(duration):
        return None
    if len(_DURATION_CACHE) >= _DURATION_CACHE_MAX:
        _DURATION_CACHE.clear()  # cheap bounded reset
    _DURATION_CACHE[key] = duration
    return duration


def segment_count(duration: float) -> int:
    """Number of fixed-length segments covering ``duration`` (last one shorter)."""
    return max(1, math.ceil(duration / SEGMENT_SECONDS))


def build_media_playlist(duration: float) -> str:
    """A complete VOD HLS media playlist for a file of ``duration`` seconds. This is
    served as the top-level playlist (the resolver points the player straight at it),
    so segment URIs are relative — ``seg{n}.ts`` resolves against the playlist path."""
    count = segment_count(duration)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i in range(count):
        start = i * SEGMENT_SECONDS
        seg_dur = min(SEGMENT_SECONDS, duration - start)
        lines.append(f"#EXTINF:{seg_dur:.6f},")
        lines.append(f"seg{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _segment_args(path: str, index: int) -> list[str]:
    start = index * SEGMENT_SECONDS
    return [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        # Fast input seek (to the keyframe at/just before `start`), then bound the
        # window. Input seek keeps long-file seeks cheap.
        "-ss", str(start),
        "-i", path,
        "-t", str(SEGMENT_SECONDS),
        # First video + first audio track (audio optional: some files have none).
        "-map", "0:v:0", "-map", "0:a:0?",
        # Video: H.264, keyframe at the segment's first frame so it decodes alone.
        "-c:v", "libx264", "-preset", _PRESET, "-crf", _CRF,
        "-pix_fmt", "yuv420p",
        "-force_key_frames", "expr:gte(t,0)",
        # Audio: stereo AAC (browser-universal).
        "-c:a", "aac", "-ac", "2", "-b:a", _AUDIO_BITRATE,
        # Place this segment's timestamps at its real position on the timeline so
        # hls.js stitches consecutive segments without gaps/overlaps.
        "-output_ts_offset", str(start),
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts", "pipe:1",
    ]


async def transcode_segment(path: str, index: int) -> Tuple[Optional[bytes], str]:
    """Transcode segment ``index`` of ``path`` to MPEG-TS bytes.

    Returns ``(data, "")`` on success or ``(None, reason)`` on failure/timeout. The
    segment is small (~one ``SEGMENT_SECONDS`` window) so it's buffered fully — that
    way a mid-transcode ffmpeg failure surfaces as a clean 5xx instead of a
    truncated 200 the player would choke on."""
    args = _segment_args(path, index)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return None, f"spawn failed: {e}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_SEGMENT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, f"timed out after {_SEGMENT_TIMEOUT}s"
    if proc.returncode != 0:
        tail = (err or b"").decode("utf-8", errors="replace").strip().splitlines()
        return None, f"ffmpeg exit {proc.returncode}: {' | '.join(tail[-3:])}"
    if not out:
        return None, "empty segment"
    return out, ""
