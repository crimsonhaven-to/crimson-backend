"""
Filesystem helpers for the server-side video cache.

Everything that touches the NAS lives here so the downloader, the scraper, the
resolver, the ``/cache_proxy`` route and the admin dashboard share one
implementation and one security model — closely mirroring ``local_engine.fs``:

  * the embed marker + proxy prefix the layers agree on,
  * opaque token <-> absolute-path encoding for ``/cache_proxy/{token}``,
  * ``safe_resolve`` — maps a token back to a real file ONLY when it currently
    lives inside an *enabled* cache target (path-traversal / symlink escapes /
    disabled targets all reject), re-checked per request,
  * ``target_for_path`` — which enabled target owns a path (so the resolver can
    label the cached source with that target's admin-given name),
  * ``plan_rel_path`` / ``language_slug`` — where a freshly downloaded episode
    lands on the NAS, organised for human browsing,
  * ``inspect_target`` — a writability + free-space + cached-file-count probe the
    Admin Dashboard shows for each target.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
from typing import Optional

from .db import CacheStore

# The scraper emits ``crimson-cache:{token}``; the resolver matches on this
# keyword and the ``/cache_proxy`` route serves it.
EMBED_MARKER = "crimson-cache"
PROXY_PREFIX = "/cache_proxy"

# We always remux to mp4, but tolerate the small browser-playable set on read in
# case the operator drops files in by hand.
WEB_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

_store = CacheStore()


# --- token <-> path ---------------------------------------------------------
def encode_token(path: str) -> str:
    """URL-safe, padding-free base64 of an absolute file path."""
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def decode_token(token: str) -> Optional[str]:
    try:
        pad = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except Exception:
        return None


def is_web_playable_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in WEB_EXTENSIONS


def media_type_for(path: str) -> str:
    return _MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


# --- the security choke point -----------------------------------------------
def _within(real_path: str, root: str) -> bool:
    """True if ``real_path`` is inside ``root`` (both fully resolved)."""
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False  # different drives / un-relatable paths (Windows)


def target_for_path(abs_path: str) -> Optional[dict]:
    """The enabled cache target whose root contains ``abs_path`` (fully resolved),
    or None. Used by the resolver to label the cached source with the target name,
    and by ``safe_resolve``."""
    real = os.path.realpath(abs_path)
    for target in _store.enabled_targets():
        if _within(real, target["path"]):
            return target
    return None


def safe_resolve(token: str) -> Optional[str]:
    """Map a ``/cache_proxy`` token back to a real file, or None.

    Returns a path ONLY when, after resolving symlinks, it is a regular,
    web-playable file living inside a *currently enabled* cache target. An
    attacker-crafted token, or one pointing at a since-disabled/removed target,
    resolves to None and 404s."""
    raw = decode_token(token)
    if not raw:
        return None
    real = os.path.realpath(raw)
    if not os.path.isfile(real) or not is_web_playable_path(real):
        return None
    return real if target_for_path(real) else None


# --- where a freshly downloaded episode lands -------------------------------
_SLUG_BAD = re.compile(r"[^A-Za-z0-9]+")


def language_slug(language: Optional[str]) -> str:
    """Filesystem-safe slug of a language label ("German Dub" -> "german-dub")."""
    s = _SLUG_BAD.sub("-", (language or "").strip().lower()).strip("-")
    return s


def plan_rel_path(
    tmdb_id: int,
    season_number: int,
    episode_number: int,
    language: Optional[str],
    container: str = "mp4",
    media_type: str = "tv",
) -> str:
    """Relative path (under a target root) for a cached file, organised for human
    browsing.

      * TV    -> ``tmdb-<id>/S<ss>E<ee>[ - <language>].<container>``
      * movie -> ``movie-tmdb-<id>/movie[ - <language>].<container>``

    Movies get the ``movie-`` dir prefix so a film never shares a directory with a
    TV show carrying the same numeric TMDB id (the two id spaces overlap) — the
    filesystem mirror of the media_type-namespaced cache key."""
    lang = (language or "").strip()
    suffix = f" - {lang}" if lang else ""
    if media_type == "movie":
        fname = f"movie{suffix}.{container}"
        return os.path.join(f"movie-tmdb-{int(tmdb_id)}", fname)
    fname = f"S{int(season_number):02d}E{int(episode_number):02d}{suffix}.{container}"
    return os.path.join(f"tmdb-{int(tmdb_id)}", fname)


# --- admin dashboard probe --------------------------------------------------
def inspect_target(path: str, count_cap: int = 5000) -> dict:
    """Existence / dir / writability + free space + (capped) cached-file count for
    the Add/Edit-Target form and the dashboard list."""
    info = {
        "exists": False,
        "is_dir": False,
        "writable": False,
        "free_bytes": None,
        "total_bytes": None,
        "file_count": 0,
        "file_count_capped": False,
    }
    try:
        if not os.path.exists(path):
            return info
        info["exists"] = True
        info["is_dir"] = os.path.isdir(path)
        info["writable"] = os.access(path, os.W_OK)
        try:
            usage = shutil.disk_usage(path)
            info["free_bytes"] = usage.free
            info["total_bytes"] = usage.total
        except Exception:
            pass
        if info["is_dir"]:
            n = 0
            capped = False
            for _root, _dirs, files in os.walk(path):
                for f in files:
                    if os.path.splitext(f)[1].lower() in WEB_EXTENSIONS:
                        n += 1
                        if n >= count_cap:
                            capped = True
                            break
                if capped:
                    break
            info["file_count"] = n
            info["file_count_capped"] = capped
    except Exception:
        pass
    return info


def is_configured() -> bool:
    """True when at least one cache target is enabled (gates the scraper/resolver
    regardless of the global download switch — a target may be disabled for new
    writes yet still serve already-cached files only while enabled)."""
    return bool(_store.enabled_targets())


def pick_write_target(min_free_bytes: int = 0) -> Optional[dict]:
    """Choose an enabled, writable target for a new download (first one with
    enough free space). Returns None when none qualify."""
    for target in _store.enabled_targets():
        info = inspect_target(target["path"], count_cap=1)
        if info["exists"] and info["is_dir"] and info["writable"]:
            free = info["free_bytes"]
            if min_free_bytes and free is not None and free < min_free_bytes:
                continue
            return target
    return None
