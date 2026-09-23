"""Path tokens and containment checks shared by the Local source and the video cache.

Tokens are the file path in padding-free urlsafe base64. They sit in URLs already
handed out, so the encoding must never change.
"""

import base64
import os
from typing import Optional

# What a browser <video> element plays as-is, so it can be range-served directly.
WEB_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def encode_token(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def decode_token(token: str) -> Optional[str]:
    try:
        pad = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except Exception:
        return None


def extension(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def is_web_playable_path(path: str) -> bool:
    return extension(path) in WEB_EXTENSIONS


def media_type_for(path: str) -> str:
    return _MEDIA_TYPES.get(extension(path), "application/octet-stream")


def is_within(real_path: str, root: str) -> bool:
    """Whether the already resolved ``real_path`` lies inside ``root``. The root is
    resolved here, so a symlinked mount point still matches."""
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


def matching_extensions(path: str, extensions: set, cap: int) -> tuple[list[str], bool]:
    """Extensions of the files under ``path`` that are in ``extensions``, and whether
    the walk stopped at ``cap``. The cap keeps a probe of a huge NAS from hanging a
    dashboard request."""
    found: list[str] = []
    for _root, _dirs, files in os.walk(path):
        for name in files:
            ext = extension(name)
            if ext in extensions:
                found.append(ext)
                if len(found) >= cap:
                    return found, True
    return found, False
