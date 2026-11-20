"""Signed links for the audio, cover and playlist cover files.

``<audio>`` and the lock screen's artwork fetch cannot send a bearer token, so
``/music_stream``, ``/music_art`` and ``/music_playlist_art`` sit outside the login wall and trust a
signature instead. A link expires, so revoking someone's music access stops
their old links within a week. The expiry is rounded to the day, so a link is
stable for a day and caches well.

A share token names one song for any signed-in member. It never expires, so a
link sent to a friend keeps working; it only opens the song while the song is
still in the library.
"""

from __future__ import annotations

import time
from functools import cache

from core import signing

STREAM = "stream"
ART = "art"
PLAYLIST_ART = "playlist_art"
SHARE = "share"
_LIFETIME_SECONDS = 7 * 86400
_DAY = 86400


@cache
def _secret() -> bytes:
    return signing.resolve_secret("MUSIC_LINK_SECRET")


def expiry() -> int:
    return (int(time.time()) + _LIFETIME_SECONDS) // _DAY * _DAY


def _payload(kind: str, item_id: int, expires: int) -> str:
    return f"music-{kind}:{item_id}:{expires}"


def signed_path(kind: str, item_id: int) -> str:
    expires = expiry()
    signature = signing.sign(_secret(), _payload(kind, item_id, expires))
    return f"/music_{kind}/{item_id}?e={expires}&s={signature}"


def verify(kind: str, item_id: int, expires: int, signature: str) -> bool:
    if expires < time.time():
        return False
    return signing.verify(_secret(), _payload(kind, item_id, expires), signature)


def share_token(track_id: int) -> str:
    return f"{track_id}.{signing.sign(_secret(), f'music-{SHARE}:{track_id}')}"


def shared_track_id(token: str) -> int | None:
    track_id, _, signature = token.partition(".")
    if not track_id.isdigit():
        return None
    if not signing.verify(_secret(), f"music-{SHARE}:{track_id}", signature):
        return None
    return int(track_id)
