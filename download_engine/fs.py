"""Where downloads land on disk: under ``<root>/crimson-downloads`` of the first
download-enabled Local source with room, staged in a dot-prefixed per-job dir the
library scanner skips.

The destination is re-checked against the enabled roots right before anything
moves, since a source can be disabled mid-download.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import List, Optional

from local_engine.db import store as local_store
from local_engine.fs import DOWNLOADS_SUBDIR, STAGING_SUBDIR, is_within_enabled_root

logger = logging.getLogger("download_engine.fs")

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name: str, fallback: str = "download") -> str:
    """An admin-supplied title reduced to one safe path segment."""
    cleaned = _UNSAFE.sub(" ", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:180].strip()
    if not cleaned or set(cleaned) == {"."}:
        return fallback
    return cleaned


def _free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def pick_write_target(min_free_bytes: int) -> Optional[dict]:
    """The first download-enabled root, in registration order, with at least
    ``min_free_bytes`` free, with its downloads and staging dirs created. None
    leaves the job pending for the next poll."""
    for root in local_store.download_roots_config():
        path = root["path"]
        if not os.path.isdir(path):
            continue
        free = _free_bytes(path)
        if free < min_free_bytes:
            continue
        downloads_dir = os.path.join(path, DOWNLOADS_SUBDIR)
        try:
            os.makedirs(os.path.join(downloads_dir, STAGING_SUBDIR), exist_ok=True)
        except OSError as e:
            logger.warning(f"[download] cannot prepare downloads dir under {path!r}: {e}")
            continue
        return {
            "id": root["id"],
            "label": root.get("label"),
            "path": path,
            "downloads_dir": downloads_dir,
            "free_bytes": free,
        }
    return None


def plan_staging_dir(downloads_dir: str, job_id: int) -> str:
    return os.path.join(downloads_dir, STAGING_SUBDIR, str(job_id))


def _unique_dest(path: str) -> str:
    """``path``, or the first free ``path (n)``, so a second download of the same
    name never clobbers the first."""
    if not os.path.exists(path):
        return path
    for n in range(2, 1000):
        cand = f"{path} ({n})"
        if not os.path.exists(cand):
            return cand
    return f"{path} ({os.getpid()})"


def _payload_entries(staging_dir: str) -> List[str]:
    """The downloaded entries, without aria2's ``.aria2`` control files."""
    try:
        names = os.listdir(staging_dir)
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and not n.endswith(".aria2")]


def _size_on_disk(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def publish(staging_dir: str, downloads_dir: str, name: Optional[str]) -> str:
    """Move a finished download out of staging and return where it landed.

    | case                  | lands at                                             |
    |-----------------------|------------------------------------------------------|
    | ``name`` given        | ``<downloads>/<name>/`` (helps metadata matching)    |
    | one entry, no name    | ``<downloads>/<entry>`` as is                        |
    | several, no name      | a folder named after the largest, so it stays one title |
    """
    entries = _payload_entries(staging_dir)
    if not entries:
        raise RuntimeError("download produced no files")
    if not is_within_enabled_root(os.path.realpath(downloads_dir)):
        raise RuntimeError("destination is no longer inside an enabled source root")

    if len(entries) == 1 and not name:
        dest = _unique_dest(os.path.join(downloads_dir, entries[0]))
        shutil.move(os.path.join(staging_dir, entries[0]), dest)
    else:
        if name:
            folder = sanitize_name(name)
        else:
            anchor = max(entries, key=lambda e: _size_on_disk(os.path.join(staging_dir, e)))
            folder = os.path.splitext(anchor)[0]
        dest = _unique_dest(os.path.join(downloads_dir, folder))
        os.makedirs(dest, exist_ok=True)
        for entry in entries:
            shutil.move(os.path.join(staging_dir, entry), os.path.join(dest, entry))

    cleanup_staging(staging_dir)
    return dest


def cleanup_staging(staging_dir: str) -> None:
    shutil.rmtree(staging_dir, ignore_errors=True)


def inspect_downloads(root_path: str) -> dict:
    """Free space and title count of a root's downloads dir, for the dashboard."""
    info = {"exists": False, "free_bytes": None, "total_bytes": None, "titles": 0}
    try:
        du = shutil.disk_usage(root_path)
        info["free_bytes"] = du.free
        info["total_bytes"] = du.total
    except OSError:
        pass
    downloads_dir = os.path.join(root_path, DOWNLOADS_SUBDIR)
    try:
        if os.path.isdir(downloads_dir):
            info["exists"] = True
            info["titles"] = sum(1 for n in os.listdir(downloads_dir) if not n.startswith("."))
    except OSError:
        pass
    return info
