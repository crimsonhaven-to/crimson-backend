"""
Filesystem helpers for the background downloader.

Everything that touches disk for the downloader lives here so the manager (and the
admin routes) share one implementation and one safety model, closely mirroring
``cache_engine.fs`` / ``local_engine.fs``:

  * ``pick_write_target`` — the first *download-enabled* local source root with
    enough free space (the "just take the first one that fits" rule the operator
    asked for), with its ``crimson-downloads`` + staging dirs ensured,
  * ``plan_staging_dir`` — the per-job dot-prefixed staging dir a download runs in
    (``<root>/crimson-downloads/.incoming/<job-id>``) so half-finished files never
    surface in the library (the scanner skips dot-dirs),
  * ``publish`` — the leech-only "move finished payload out of staging into
    ``crimson-downloads``" step, with an on-disk-name collision guard,
  * ``sanitize_name`` — turn an admin-supplied title into a safe single path
    segment,
  * ``inspect_downloads`` — a free-space + occupancy probe of a source's downloads
    dir for the dashboard.

The destination is always recomputed from a *currently* download-enabled root and
re-checked with ``local_engine.fs.is_within_enabled_root`` before any move, so a
job can never write outside a registered source root.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import List, Optional

from local_engine.fs import (
    DOWNLOADS_SUBDIR,
    STAGING_SUBDIR,
    download_roots_config,
    is_within_enabled_root,
)

logger = logging.getLogger("download_engine.fs")


# --- name hygiene -----------------------------------------------------------
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name: str, fallback: str = "download") -> str:
    """Reduce an admin-supplied title to a single safe path segment (no separators,
    no traversal, bounded length)."""
    cleaned = _UNSAFE.sub(" ", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:180].strip()
    # Never let a name become "." / ".." / empty after stripping.
    if not cleaned or set(cleaned) == {"."}:
        return fallback
    return cleaned


# --- target selection -------------------------------------------------------
def _free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def pick_write_target(min_free_bytes: int) -> Optional[dict]:
    """The first download-enabled source root with at least ``min_free_bytes`` free,
    in registration order — the operator's "take the first one that fits" rule.

    Returns ``{"id", "label", "path", "downloads_dir", "free_bytes"}`` with the
    ``crimson-downloads`` (and its ``.incoming`` staging) directories created, or None
    when no enabled+download-enabled root has room (the job stays pending and is
    retried). Blocking disk I/O — call via run_in_threadpool."""
    for root in download_roots_config():
        base = root["path"]
        if not os.path.isdir(base):
            continue
        if _free_bytes(base) < min_free_bytes:
            continue
        downloads_dir = os.path.join(base, DOWNLOADS_SUBDIR)
        try:
            os.makedirs(os.path.join(downloads_dir, STAGING_SUBDIR), exist_ok=True)
        except OSError as e:
            logger.warning(f"[download] cannot prepare downloads dir under {base!r}: {e}")
            continue
        return {
            "id": root["id"],
            "label": root.get("label"),
            "path": base,
            "downloads_dir": downloads_dir,
            "free_bytes": _free_bytes(base),
        }
    return None


def plan_staging_dir(downloads_dir: str, job_id: int) -> str:
    """Per-job staging dir aria2 downloads into: ``<downloads>/.incoming/<job-id>``.
    Dot-prefixed so the library scanner skips it while the download is in flight."""
    return os.path.join(downloads_dir, STAGING_SUBDIR, str(job_id))


# --- publish (leech-only: move finished payload into place) ------------------
def _unique_dest(path: str) -> str:
    """``path`` if free, else the first ``path (2)``, ``path (3)`` … that isn't taken,
    so a second download of the same name never clobbers the first."""
    if not os.path.exists(path):
        return path
    base = path
    for n in range(2, 1000):
        cand = f"{base} ({n})"
        if not os.path.exists(cand):
            return cand
    return f"{base} ({os.getpid()})"


def _payload_entries(staging_dir: str) -> List[str]:
    """Names of the real downloaded entries in a staging dir — everything except
    aria2's own ``.aria2`` control files and dotfiles."""
    try:
        names = os.listdir(staging_dir)
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and not n.endswith(".aria2")]


def publish(staging_dir: str, downloads_dir: str, name: Optional[str]) -> str:
    """Move a finished download out of staging into ``crimson-downloads`` and return
    the published path. Leech-only: aria2 has stopped by now; we just relocate the
    payload so the scanner picks it up.

    * ``name`` given  -> everything lands in ``<downloads>/<name>/`` (a titled folder,
      which greatly helps metadata identification).
    * ``name`` absent -> each top-level entry is moved directly under ``<downloads>/``
      as-is: a torrent keeps its release-folder name, and a single loose file becomes a
      one-file movie title the scanner recognises.

    The final directory is re-validated to sit inside a currently enabled source root
    before anything moves. Blocking — call via run_in_threadpool."""
    entries = _payload_entries(staging_dir)
    if not entries:
        raise RuntimeError("download produced no files")

    # Guard: the downloads dir must still be inside an enabled root (a source could
    # have been disabled mid-download).
    if not is_within_enabled_root(os.path.realpath(downloads_dir)):
        raise RuntimeError("destination is no longer inside an enabled source root")

    if name:
        dest = _unique_dest(os.path.join(downloads_dir, sanitize_name(name)))
        os.makedirs(dest, exist_ok=True)
        for entry in entries:
            shutil.move(os.path.join(staging_dir, entry), os.path.join(dest, entry))
        published = dest
    elif len(entries) == 1:
        src = os.path.join(staging_dir, entries[0])
        dest = _unique_dest(os.path.join(downloads_dir, entries[0]))
        shutil.move(src, dest)
        published = dest
    else:
        # Multiple loose entries with no wrapping name: keep them together under a
        # folder named after the largest entry so they stay one browsable title.
        anchor = max(entries, key=lambda e: _size_on_disk(os.path.join(staging_dir, e)))
        dest = _unique_dest(os.path.join(downloads_dir, os.path.splitext(anchor)[0]))
        os.makedirs(dest, exist_ok=True)
        for entry in entries:
            shutil.move(os.path.join(staging_dir, entry), os.path.join(dest, entry))
        published = dest

    cleanup_staging(staging_dir)
    return published


def cleanup_staging(staging_dir: str) -> None:
    """Remove a job's staging dir (leftover ``.aria2`` control files and any partial
    data). Best-effort — never raises."""
    try:
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        pass


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


# --- admin dashboard probe --------------------------------------------------
def inspect_downloads(root_path: str) -> dict:
    """Free-space + occupancy of a source's downloads dir, for the dashboard."""
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
            info["titles"] = sum(
                1 for n in os.listdir(downloads_dir) if not n.startswith(".")
            )
    except OSError:
        pass
    return info
