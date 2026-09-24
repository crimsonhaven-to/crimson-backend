"""Writing the final ``.m4a``: Spotify's metadata and cover into the downloaded
audio. Tags come from the import, never from the source's own video title,
which is the point of doing this pass at all. A song added by search has no
import behind it; the worker hands in the source's music metadata instead."""

from __future__ import annotations

import os
from typing import Optional

from core import ffmpeg

TIMEOUT_SECONDS = 300.0


def tag_args(source: str, cover: Optional[str], dest: str, track: dict, album: str) -> list[str]:
    """AAC from the source is copied untouched; anything else is encoded once
    at 256k, high enough that the second lossy generation is inaudible."""
    args = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", source]
    if cover:
        args += ["-i", cover]
    args += ["-map", "0:a:0"]
    if cover:
        args += ["-map", "1:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    if os.path.splitext(source)[1].lower() in (".m4a", ".mp4", ".aac"):
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", "256k"]

    artists = track.get("artists") or []

    def meta(key: str, value: object) -> None:
        if value:
            args.extend(["-metadata", f"{key}={value}"])

    meta("title", track.get("title"))
    meta("artist", ", ".join(artists))
    meta("album", album)
    meta("album_artist", track.get("album_artist") or (artists[0] if artists else ""))
    meta("track", track.get("track_number"))
    meta("disc", track.get("disc_number"))
    meta("date", (track.get("release_date") or "")[:4])
    return args + ["-movflags", "+faststart", "-f", "mp4", dest]


def cover_args(source: str, dest: str) -> list[str]:
    """Whatever the image was (a search result's thumbnail is WebP, which MP4
    cannot carry, and 16:9), the file gets a square JPEG cut from its centre,
    which is also where YouTube puts a song's album art."""
    return [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", source,
        "-vf", "crop='min(iw,ih)':'min(iw,ih)'", "-frames:v", "1", "-q:v", "2", "-f", "mjpeg",
        dest,
    ]


async def square_jpeg(source: str, dest: str) -> bool:
    rc, _out, _lines = await ffmpeg.run(cover_args(source, dest), timeout=60.0)
    return rc == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0


async def write_tagged(
    source: str, cover: Optional[str], dest: str, track: dict, album: str
) -> Optional[str]:
    """None on success, else the error worth showing."""
    rc, _out, lines = await ffmpeg.run(
        tag_args(source, cover, dest, track, album), timeout=TIMEOUT_SECONDS
    )
    if rc is None:
        return f"tagging timed out after {int(TIMEOUT_SECONDS)}s"
    if rc != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
        return f"ffmpeg exit {rc}: {' | '.join(lines[-3:])}"
    return None
