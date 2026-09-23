"""Stateless on-the-fly HLS for Local files a browser cannot play directly.

The playlist is computed from the duration alone (fixed-length segments), and each
``seg{n}.ts`` request runs one short ffmpeg that seeks to ``n * SEGMENT_SECONDS``
and encodes just that window. No session state means any replica can serve any
segment and a seek is just another segment number; the price is one ffmpeg spawn
per segment.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
from typing import Optional, Tuple

from core import ffmpeg
from core.bounded_cache import BoundedCache

logger = logging.getLogger("local_engine.transcode")

# The HLS convention: long enough to amortise the per-segment ffmpeg spawn, short
# enough to keep seeks snappy.
SEGMENT_SECONDS = 6

# ``veryfast`` keeps one 1080p transcode within about one core, the api
# container's budget.
_PRESET = "veryfast"
_CRF = "21"
_AUDIO_BITRATE = "160k"

# A 6s segment that needs more than this to encode is broken, not slow, and must
# not pin a worker forever.
_SEGMENT_TIMEOUT = 120

# Every segment request re-checks the segment count, so the probe is memoised.
# Keyed by (path, mtime, size) so a replaced file is probed again.
_durations = BoundedCache(4096)


def tools_available() -> bool:
    """Encoding needs ffprobe for the duration as well as ffmpeg. The dashboard
    greys the toggle out otherwise."""
    return ffmpeg.ffmpeg_available() and ffmpeg.ffprobe_available()


def probe_duration(path: str) -> Optional[float]:
    """Seconds, or None when the file cannot be probed. Blocking."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, int(st.st_mtime), st.st_size)
    cached = _durations.get(key)
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
    _durations.set(key, duration)
    return duration


def segment_count(duration: float) -> int:
    return max(1, math.ceil(duration / SEGMENT_SECONDS))


def build_media_playlist(duration: float) -> str:
    """Served as the top-level playlist, so the relative ``seg{n}.ts`` URIs resolve
    against its own path."""
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
        # Input seek (before -i) keeps seeks into long files cheap.
        "-ss", str(start),
        "-i", path,
        "-t", str(SEGMENT_SECONDS),
        # Some files have no audio track, hence the optional map.
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", _PRESET, "-crf", _CRF,
        "-pix_fmt", "yuv420p",
        # A keyframe on the first frame lets each segment decode on its own.
        "-force_key_frames", "expr:gte(t,0)",
        "-c:a", "aac", "-ac", "2", "-b:a", _AUDIO_BITRATE,
        # Timestamps at the segment's real position, so hls.js stitches segments
        # without gaps or overlaps.
        "-output_ts_offset", str(start),
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts", "pipe:1",
    ]


async def transcode_segment(path: str, index: int) -> Tuple[Optional[bytes], str]:
    """``(data, "")`` or ``(None, reason)``. The segment is buffered whole so an
    ffmpeg failure midway becomes a clean 5xx, not a truncated 200 the player
    would choke on."""
    try:
        rc, out, lines = await ffmpeg.run(
            _segment_args(path, index), timeout=_SEGMENT_TIMEOUT, capture_stdout=True
        )
    except OSError as e:
        return None, f"spawn failed: {e}"
    if rc is None:
        return None, f"timed out after {_SEGMENT_TIMEOUT}s"
    if rc != 0:
        return None, f"ffmpeg exit {rc}: {' | '.join(lines[-3:])}"
    if not out:
        return None, "empty segment"
    return out, ""
