"""Disk access for the admin-managed Local source, and its one security model.

Every path a client hands back arrives as a token, and each ``safe_resolve*``
maps it to a file only while that file sits inside a currently enabled source
root, re-checked per request. Disabling a root (or its encoding switch) therefore
cuts off its streams at once, and a crafted token or symlink cannot escape.
"""

from __future__ import annotations

import os
from functools import cache
from typing import List, Optional

from core import signing
from core.media_paths import (
    WEB_EXTENSIONS,
    decode_token,
    encode_token,
    extension,
    is_web_playable_path,
    is_within,
    matching_extensions,
    media_type_for as media_type_for,
)

from .db import store

# The scraper emits ``crimson-local:{token}``. The resolver serves a web-native
# file from /local_proxy and anything else from /local_hls, the latter only when
# the file's root has encoding on.
EMBED_MARKER = "crimson-local"
PROXY_PREFIX = "/local_proxy"
HLS_PREFIX = "/local_hls"
# Public, because an <img> cannot carry the login-wall bearer, so every URL is
# HMAC-signed instead.
ART_PREFIX = "/local_art"

# The downloader publishes into ``<root>/crimson-downloads/<title>`` and stages in
# progress work under the dot-prefixed dir, which the library scanner skips.
DOWNLOADS_SUBDIR = "crimson-downloads"
STAGING_SUBDIR = ".incoming"

# Containers ffmpeg can read but a browser cannot. A web-native container is
# deliberately absent: it always takes the cheaper direct-play path.
TRANSCODE_EXTENSIONS = {
    ".mkv", ".avi", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2v", ".vob", ".ogv", ".ogm", ".3gp", ".divx", ".mxf", ".rmvb",
}

ART_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_ART_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def is_configured() -> bool:
    return bool(store.enabled_roots())


def is_transcodable_path(path: str) -> bool:
    return extension(path) in TRANSCODE_EXTENSIONS


def is_art_path(path: str) -> bool:
    return extension(path) in ART_EXTENSIONS


def art_media_type_for(path: str) -> str:
    return _ART_MEDIA_TYPES.get(extension(path), "application/octet-stream")


def _enabled_root_for(real_path: str) -> Optional[dict]:
    for root in store.enabled_roots_config():
        if is_within(real_path, root["path"]):
            return root
    return None


def is_within_enabled_root(real_path: str) -> bool:
    return _enabled_root_for(real_path) is not None


def source_label_for(real_path: str) -> Optional[str]:
    root = _enabled_root_for(real_path)
    return root.get("label") if root else None


def encoding_enabled_for(real_path: str) -> bool:
    root = _enabled_root_for(real_path)
    return bool(root and root["encoding"])


def is_playable_path(path: str) -> bool:
    """Web-native files always; transcodable ones only under a root with encoding
    on, so a root without it never lists files it cannot play."""
    if is_web_playable_path(path):
        return True
    return is_transcodable_path(path) and encoding_enabled_for(os.path.realpath(path))


def _real_path(token: str) -> Optional[str]:
    raw = decode_token(token)
    return os.path.realpath(raw) if raw else None


def safe_resolve(token: str) -> Optional[str]:
    real = _real_path(token)
    if real and os.path.isfile(real) and is_web_playable_path(real) and is_within_enabled_root(real):
        return real
    return None


def safe_resolve_transcode(token: str) -> Optional[str]:
    real = _real_path(token)
    if real and os.path.isfile(real) and is_transcodable_path(real) and encoding_enabled_for(real):
        return real
    return None


def safe_resolve_dir(token: str) -> Optional[str]:
    real = _real_path(token)
    if real and os.path.isdir(real) and is_within_enabled_root(real):
        return real
    return None


@cache
def _art_secret() -> bytes:
    return signing.resolve_secret("LOCAL_PROXY_SECRET")


def art_proxy_url(path: str) -> str:
    """Relative ``/local_art`` URL; callers absolutise it like the other proxy paths."""
    token = encode_token(path)
    return f"{ART_PREFIX}?f={token}&s={signing.sign(_art_secret(), token)}"


def safe_resolve_art(token: str, sig: str) -> Optional[str]:
    if not token or not signing.verify(_art_secret(), token, sig):
        return None
    real = _real_path(token)
    if real and os.path.isfile(real) and is_art_path(real) and is_within_enabled_root(real):
        return real
    return None


def inspect_path(path: str, *, count_cap: int = 2000) -> dict:
    """Existence, readability and a capped count of playable files, so the admin
    sees at once whether a path holds media."""
    info = {
        "exists": False,
        "is_dir": False,
        "readable": False,
        "video_count": 0,
        "transcodable_count": 0,
        "video_count_capped": False,
    }
    if not os.path.exists(path):
        return info
    info["exists"] = True
    info["is_dir"] = os.path.isdir(path)
    info["readable"] = os.access(path, os.R_OK)
    if info["is_dir"] and info["readable"]:
        found, capped = matching_extensions(path, WEB_EXTENSIONS | TRANSCODE_EXTENSIONS, count_cap)
        web = sum(1 for ext in found if ext in WEB_EXTENSIONS)
        info["video_count"] = web
        info["transcodable_count"] = len(found) - web
        info["video_count_capped"] = capped
    return info


_PSEUDO_FS = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "mqueue",
    "overlay", "shm", "securityfs", "pstore", "bpf", "tracefs", "debugfs",
    "configfs", "fusectl", "ramfs", "autofs", "binfmt_misc", "hugetlbfs", "nsfs",
}
_SYSTEM_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/etc", "/boot", "/var/lib", "/var/run",
    "/usr", "/tmp", "/snap", "/lib", "/lib64", "/sbin", "/bin",
)
# Where media bind-mounts usually land. Covers local dev, where /proc/mounts
# says nothing useful.
_COMMON_BASES = ("/media", "/mnt", "/crimson", "/movies", "/data", "/library", "/storage")


def discover_mountpoints() -> List[dict]:
    """Candidate media directories for the dashboard, each with an ``inspect_path``
    probe. A Docker bind-mount shows up in /proc/mounts under its in-container
    path, which is exactly what the admin has to register."""
    found: dict = {}
    # Advisory only: a missing or odd /proc/mounts just means fewer suggestions.
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                # /proc/mounts escapes spaces and the like as octal (\040).
                mnt = parts[1].encode("ascii", "ignore").decode("unicode_escape")
                fstype = parts[2]
                if fstype in _PSEUDO_FS or mnt == "/":
                    continue
                if any(mnt == p or mnt.startswith(p + "/") for p in _SYSTEM_PREFIXES):
                    continue
                found.setdefault(mnt, fstype)
    except Exception:
        pass

    for base in _COMMON_BASES:
        try:
            if os.path.isdir(base):
                for name in sorted(os.listdir(base)):
                    p = os.path.join(base, name)
                    if os.path.isdir(p):
                        found.setdefault(p, "dir")
        except OSError:
            continue

    return [
        {"path": path, "fstype": fstype, **inspect_path(path, count_cap=200)}
        for path, fstype in sorted(found.items())
    ]
