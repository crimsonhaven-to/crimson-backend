"""Signed links for the audio and cover files.

``<audio>`` and the lock screen's artwork fetch cannot send a bearer token, so
``/music_stream`` and ``/music_art`` sit outside the login wall and trust a
signature instead. A link expires, so revoking someone's music access stops
their old links within a week. The expiry is rounded to the day, so a link is
stable for a day and caches well.
"""

from __future__ import annotations

import time
from functools import cache

from core import signing

STREAM = "stream"
ART = "art"
_LIFETIME_SECONDS = 7 * 86400
_DAY = 86400


@cache
def _secret() -> bytes:
    return signing.resolve_secret("MUSIC_LINK_SECRET")


def expiry() -> int:
    return (int(time.time()) + _LIFETIME_SECONDS) // _DAY * _DAY


def _payload(kind: str, track_id: int, expires: int) -> str:
    return f"music-{kind}:{track_id}:{expires}"


def signed_path(kind: str, track_id: int) -> str:
    expires = expiry()
    signature = signing.sign(_secret(), _payload(kind, track_id, expires))
    return f"/music_{kind}/{track_id}?e={expires}&s={signature}"


def verify(kind: str, track_id: int, expires: int, signature: str) -> bool:
    if expires < time.time():
        return False
    return signing.verify(_secret(), _payload(kind, track_id, expires), signature)
