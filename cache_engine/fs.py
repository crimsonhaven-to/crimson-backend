"""Where cached episodes live on the NAS, and the check that serves them.

A ``/cache_proxy`` token is resolved to a file only while it sits inside a
currently enabled cache target, re-checked on every request, so disabling a
target hides its files at once and a crafted token cannot escape the targets.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from core.media_paths import (
    WEB_EXTENSIONS,
    decode_token,
    encode_token as encode_token,
    is_web_playable_path,
    is_within,
    matching_extensions,
    media_type_for as media_type_for,
)

from .db import store

# The scraper emits ``crimson-cache:{token}``; the resolver matches the keyword.
EMBED_MARKER = "crimson-cache"
PROXY_PREFIX = "/cache_proxy"


def target_for_path(abs_path: str) -> Optional[dict]:
    """The enabled target containing ``abs_path``. The resolver labels the cached
    source with its name."""
    real = os.path.realpath(abs_path)
    for target in store.enabled_targets():
        if is_within(real, target["path"]):
            return target
    return None


def safe_resolve(token: str) -> Optional[str]:
    raw = decode_token(token)
    if not raw:
        return None
    real = os.path.realpath(raw)
    if not os.path.isfile(real) or not is_web_playable_path(real):
        return None
    return real if target_for_path(real) else None


def plan_rel_path(
    tmdb_id: int,
    season_number: int,
    episode_number: int,
    language: Optional[str],
    container: str = "mp4",
    media_type: str = "tv",
) -> str:
    """``tmdb-<id>/S<ss>E<ee>[ - <language>].<ext>`` for TV,
    ``movie-tmdb-<id>/movie[ - <language>].<ext>`` for movies.

    TMDB numbers movies and shows independently, so the ``movie-`` prefix keeps a
    film out of a same-id show's folder. The Local library scanner recognises both
    folder names."""
    lang = (language or "").strip()
    suffix = f" - {lang}" if lang else ""
    if media_type == "movie":
        return os.path.join(f"movie-tmdb-{int(tmdb_id)}", f"movie{suffix}.{container}")
    fname = f"S{int(season_number):02d}E{int(episode_number):02d}{suffix}.{container}"
    return os.path.join(f"tmdb-{int(tmdb_id)}", fname)


def inspect_target(path: str, count_cap: int = 5000) -> dict:
    """Writability, free space and a capped count of cached files, for the dashboard."""
    info = {
        "exists": False,
        "is_dir": False,
        "writable": False,
        "free_bytes": None,
        "total_bytes": None,
        "file_count": 0,
        "file_count_capped": False,
    }
    if not os.path.exists(path):
        return info
    info["exists"] = True
    info["is_dir"] = os.path.isdir(path)
    info["writable"] = os.access(path, os.W_OK)
    try:
        usage = shutil.disk_usage(path)
        info["free_bytes"] = usage.free
        info["total_bytes"] = usage.total
    except OSError:
        pass
    if info["is_dir"]:
        found, capped = matching_extensions(path, WEB_EXTENSIONS, count_cap)
        info["file_count"] = len(found)
        info["file_count_capped"] = capped
    return info


def is_configured() -> bool:
    return bool(store.enabled_targets())


def pick_write_target(min_free_bytes: int = 0) -> Optional[dict]:
    """The first enabled, writable target with at least ``min_free_bytes`` free."""
    for target in store.enabled_targets():
        path = target["path"]
        if not os.path.isdir(path) or not os.access(path, os.W_OK):
            continue
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            free = None
        if min_free_bytes and free is not None and free < min_free_bytes:
            continue
        return target
    return None
