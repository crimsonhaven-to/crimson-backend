"""
Filesystem helpers for the admin-managed "Local" media source.

Everything that touches the disk lives here so the scraper, the resolver, the
``/local_proxy`` route and the admin dashboard all share one implementation and,
crucially, one security model:

  * the embed marker + proxy prefix the layers agree on,
  * which extensions are direct-playable in a ``<video>`` tag — MVP scope is
    direct play only (no transcoding), so the list is intentionally small,
  * opaque token <-> absolute-path encoding for the ``/local_proxy/{token}`` URL,
  * ``safe_resolve`` — the choke point that maps a token back to a real file ONLY
    when it currently lives inside an *enabled* source root (path traversal and
    symlink escapes are rejected; re-checked per request),
  * ``inspect_path`` / ``discover_mountpoints`` — read-only helpers the Admin
    Dashboard uses to validate a path and to surface Docker/NAS mounts as
    one-click suggestions.
"""

from __future__ import annotations

import base64
import os
from typing import List, Optional

from .db import LocalSourceStore

# The scraper emits ``crimson-local:{token}``; the resolver matches on this
# keyword and serves it via one of two routes depending on the file + the source's
# per-root ``encoding`` flag:
#   * ``/local_proxy``  — direct play (Range-served bytes) for browser-native files.
#   * ``/local_hls``    — on-the-fly HLS transcode for everything else, but ONLY
#                         when the file's source has encoding enabled.
EMBED_MARKER = "crimson-local"
PROXY_PREFIX = "/local_proxy"
HLS_PREFIX = "/local_hls"

# A browser ``<video>`` element can play these as-is, so the backend just
# range-serves the bytes (the ``/local_proxy`` direct-play path).
WEB_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}

# Containers a browser can't play directly but ffmpeg can read — surfaced only
# when the source has ``encoding`` enabled, and streamed through the ``/local_hls``
# transcode route (remuxed/re-encoded to HLS on the fly). Note ``.mp4``/``.m4v``/
# ``.mov``/``.webm`` are deliberately NOT here: a web-native container always takes
# the cheaper direct-play path even on an encoding-enabled source.
TRANSCODE_EXTENSIONS = {
    ".mkv", ".avi", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2v", ".vob", ".ogv", ".ogm", ".3gp", ".divx", ".mxf", ".rmvb",
}

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

_store = LocalSourceStore()


# --- config -----------------------------------------------------------------
def is_configured() -> bool:
    """True when at least one local source is enabled (gates scraper/resolver)."""
    return bool(_store.enabled_roots())


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


def is_transcodable_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in TRANSCODE_EXTENSIONS


def media_type_for(path: str) -> str:
    return _MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _encoding_root_for(real_path: str) -> Optional[dict]:
    """The enabled source root that contains ``real_path`` (already fully resolved),
    or None. Returns the whole config entry so callers can read its ``encoding`` flag
    without a second lookup."""
    for root in _store.enabled_roots_config():
        if _within(real_path, root["path"]):
            return root
    return None


def encoding_enabled_for(real_path: str) -> bool:
    """True when ``real_path`` lives in an enabled source root that has encoding on."""
    root = _encoding_root_for(real_path)
    return bool(root and root["encoding"])


def is_playable_path(path: str) -> bool:
    """Whether the Local source should surface ``path`` at all: a web-native file
    (always), or a transcodable container whose source has encoding enabled. Used by
    the scraper so a disabled-encoding root never lists files it can't actually play."""
    if is_web_playable_path(path):
        return True
    return is_transcodable_path(path) and encoding_enabled_for(os.path.realpath(path))


# --- the security choke point -----------------------------------------------
def _within(real_path: str, root: str) -> bool:
    """True if ``real_path`` is inside ``root`` (both fully resolved)."""
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        # Different drives (Windows) / un-relatable paths.
        return False


def safe_resolve(token: str) -> Optional[str]:
    """Map a ``/local_proxy`` token back to a real file, or None.

    Returns a path ONLY when, after fully resolving symlinks, it is a regular,
    web-playable file living inside a *currently enabled* source root. This is
    what makes the proxy safe: an attacker-crafted token (``../../etc/passwd``,
    a symlink out of the library, a path under a since-disabled source) resolves
    to None and 404s.
    """
    raw = decode_token(token)
    if not raw:
        return None
    real = os.path.realpath(raw)
    if not os.path.isfile(real) or not is_web_playable_path(real):
        return None
    for root in _store.enabled_roots():
        if _within(real, root):
            return real
    return None


def safe_resolve_transcode(token: str) -> Optional[str]:
    """The ``/local_hls`` counterpart of :func:`safe_resolve`.

    Maps a token back to a real file ONLY when, after resolving symlinks, it is a
    regular *transcodable* file living inside a *currently enabled* source root that
    has **encoding turned on**. So flipping a source's encoding off (or disabling the
    source) instantly 404s its transcode streams, re-checked on every segment
    request — the same per-request safety model as the direct-play path."""
    raw = decode_token(token)
    if not raw:
        return None
    real = os.path.realpath(raw)
    if not os.path.isfile(real) or not is_transcodable_path(real):
        return None
    return real if encoding_enabled_for(real) else None


# --- admin dashboard helpers (read-only) ------------------------------------
def inspect_path(path: str, *, count_cap: int = 2000) -> dict:
    """Quick, bounded health probe of a path for the Add-Source form.

    Reports existence / dir / readability and a (capped) count of direct-playable
    video files beneath it, so the admin gets immediate feedback that the path is
    valid and actually holds media. Capped so registering a huge NAS doesn't hang.
    """
    info = {
        "exists": False,
        "is_dir": False,
        "readable": False,
        "video_count": 0,            # direct-playable (web-native) files
        "transcodable_count": 0,     # files that need encoding on to play
        "video_count_capped": False,
    }
    try:
        if not os.path.exists(path):
            return info
        info["exists"] = True
        info["is_dir"] = os.path.isdir(path)
        info["readable"] = os.access(path, os.R_OK)
        if info["is_dir"] and info["readable"]:
            n = 0          # web-native
            t = 0          # transcodable
            capped = False
            for _root, _dirs, files in os.walk(path):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in WEB_EXTENSIONS:
                        n += 1
                    elif ext in TRANSCODE_EXTENSIONS:
                        t += 1
                    if (n + t) >= count_cap:
                        capped = True
                        break
                if capped:
                    break
            info["video_count"] = n
            info["transcodable_count"] = t
            info["video_count_capped"] = capped
    except Exception:
        pass
    return info


# Pseudo / virtual filesystems and system mount points we never want to suggest.
_PSEUDO_FS = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "mqueue",
    "overlay", "shm", "securityfs", "pstore", "bpf", "tracefs", "debugfs",
    "configfs", "fusectl", "ramfs", "autofs", "binfmt_misc", "hugetlbfs", "nsfs",
}
_SYSTEM_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/etc", "/boot", "/var/lib", "/var/run",
    "/usr", "/tmp", "/snap", "/lib", "/lib64", "/sbin", "/bin",
)
# Common bases a media bind-mount tends to land under (covers simple setups and
# local dev where /proc/mounts isn't informative).
_COMMON_BASES = ("/media", "/mnt", "/crimson", "/movies", "/data", "/library", "/storage")


def discover_mountpoints() -> List[dict]:
    """Best-effort list of candidate media directories for the dashboard.

    Surfaces Docker bind-mounts (``-v /movies:/crimson/movies1`` shows up as the
    in-container mount point ``/crimson/movies1``) by reading ``/proc/mounts``,
    plus the immediate children of a few conventional media bases. Pseudo and
    system mounts are filtered out. Each entry carries an ``inspect_path`` probe
    so the admin can tell which one holds the library. Purely advisory — the admin
    can always type a path by hand.
    """
    found: dict = {}

    # 1) /proc/mounts (Linux / inside Docker) — the reliable source for binds.
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                # mount points escape spaces etc. as octal (\040); unescape them.
                mnt = parts[1].encode("ascii", "ignore").decode("unicode_escape")
                fstype = parts[2]
                if fstype in _PSEUDO_FS or mnt == "/":
                    continue
                if any(mnt == p or mnt.startswith(p + "/") for p in _SYSTEM_PREFIXES):
                    continue
                found.setdefault(mnt, fstype)
    except FileNotFoundError:
        pass  # not Linux (e.g. local Windows dev) — fall back to the bases below
    except Exception:
        pass

    # 2) immediate subdirectories of conventional media bases.
    for base in _COMMON_BASES:
        try:
            if os.path.isdir(base):
                for name in sorted(os.listdir(base)):
                    p = os.path.join(base, name)
                    if os.path.isdir(p):
                        found.setdefault(p, "dir")
        except Exception:
            continue

    out = []
    for path, fstype in sorted(found.items()):
        out.append({"path": path, "fstype": fstype, **inspect_path(path, count_cap=200)})
    return out
