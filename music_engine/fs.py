"""Where the library lives on the music share.

The layout is for people, not for this app, so the files stay useful if the
share is ever pointed at another player (Jellyfin, Navidrome, a car's USB
stick)::

    <root>/Artist/Album/01 - Title.m4a
    <root>/Artist/Album/cover.jpg
    <root>/Playlists/<owner>/<name>.m3u8
    <root>/.incoming/<track id>/          work files, moved into place when done

The share is SMB on Windows, so names are cleaned to what Windows accepts.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Optional

from core.config import get_settings
from core.media_paths import is_within

INCOMING = ".incoming"
PLAYLISTS = "Playlists"
_MAX_SEGMENT = 100

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)}


def root() -> str:
    return get_settings().music_root


def available() -> bool:
    path = root()
    return bool(path) and os.path.isdir(path) and os.access(path, os.W_OK)


def safe_segment(text: str, fallback: str) -> str:
    """One path segment Windows will take: no reserved characters, no trailing
    dot or space, not a device name."""
    cleaned = _UNSAFE.sub("_", text or "").strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)[:_MAX_SEGMENT].rstrip(". ")
    if not cleaned or cleaned.lower().split(".")[0] in _RESERVED:
        return fallback
    return cleaned


def plan_rel_path(track: dict) -> str:
    """``Album Artist/Album/[D-]NN - Title.m4a``. A track with no album goes
    under ``Singles``; one with no number is named by its title alone."""
    artists = track.get("artists") or []
    artist = safe_segment(track.get("album_artist") or (artists[0] if artists else ""), "Unknown Artist")
    album = safe_segment(track.get("album") or "", "Singles")
    title = safe_segment(track.get("title") or "", "Untitled")
    number = track.get("track_number")
    disc = track.get("disc_number")
    if number:
        prefix = f"{disc}-{number:02d}" if disc and disc > 1 else f"{number:02d}"
        name = f"{prefix} - {title}"
    else:
        name = title
    return os.path.join(artist, album, f"{name}.m4a")


def absolute(rel_path: str) -> Optional[str]:
    """The file behind ``rel_path``, only while it stays inside the root, so a
    row can never be made to point outside the share."""
    base = root()
    if not base or not rel_path:
        return None
    real = os.path.realpath(os.path.join(base, rel_path))
    return real if is_within(real, base) else None


def unique_rel_path(rel_path: str) -> str:
    """``rel_path``, or ``name (2).m4a`` and up when another recording already
    took the name. Each track is placed once, so an existing file is never ours."""
    stem, ext = os.path.splitext(rel_path)
    candidate, n = rel_path, 1
    while os.path.exists(os.path.join(root(), candidate)):
        n += 1
        candidate = f"{stem} ({n}){ext}"
    return candidate


def work_dir(track_id: int) -> str:
    """A fresh directory on the share itself, so publishing is an atomic rename."""
    path = os.path.join(root(), INCOMING, str(track_id))
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


def remove_work_dir(track_id: int) -> None:
    shutil.rmtree(os.path.join(root(), INCOMING, str(track_id)), ignore_errors=True)


def publish(work_file: str, rel_path: str) -> str:
    dest = os.path.join(root(), rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(work_file, dest)
    return dest


def place_cover(work_cover: str, rel_path: str) -> str:
    """``cover.jpg`` beside the track, kept if the album already has one."""
    rel_cover = os.path.join(os.path.dirname(rel_path), "cover.jpg")
    dest = os.path.join(root(), rel_cover)
    if not os.path.exists(dest):
        shutil.copyfile(work_cover, dest + ".part")
        os.replace(dest + ".part", dest)
    return rel_cover


def write_playlist_file(owner: str, name: str, entries: list[dict]) -> str:
    """An extended M3U next to the library, paths relative to the file so the
    share can be mounted anywhere. Written whole and renamed into place."""
    rel_dir = os.path.join(PLAYLISTS, safe_segment(owner, "unknown"))
    directory = os.path.join(root(), rel_dir)
    os.makedirs(directory, exist_ok=True)
    lines = ["#EXTM3U"]
    for entry in entries:
        artists = ", ".join(entry.get("artists") or [])
        seconds = int((entry.get("duration_ms") or 0) / 1000) or -1
        lines.append(f"#EXTINF:{seconds},{artists} - {entry.get('title') or ''}")
        target = os.path.join(root(), entry["rel_path"])
        lines.append(os.path.relpath(target, directory).replace(os.sep, "/"))
    rel_file = os.path.join(rel_dir, f"{safe_segment(name, 'Playlist')}.m3u8")
    dest = os.path.join(root(), rel_file)
    with open(dest + ".part", "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(dest + ".part", dest)
    return rel_file
