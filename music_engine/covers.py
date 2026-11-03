"""Checking an uploaded playlist cover. The type is read from the bytes, never
from the request, because the file is later served back under that type."""

from __future__ import annotations

# The client shrinks a picture to a few hundred kilobytes before sending it, so
# anything near this is not coming from the app.
MAX_BYTES = 2 * 1024 * 1024

_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class CoverError(Exception):
    """A reason the user can act on."""


def content_type(data: bytes) -> str:
    if not data:
        raise CoverError("That picture is empty.")
    if len(data) > MAX_BYTES:
        raise CoverError("That picture is too large. Covers can be up to 2 MB.")
    for magic, kind in _SIGNATURES:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise CoverError("Covers can be JPEG, PNG or WebP pictures.")
