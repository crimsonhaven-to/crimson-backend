"""Signed download links for the desktop app's installers.

A browser download is a plain navigation and cannot send a bearer token, so
``/app/download`` sits outside the login wall and trusts a signature instead.
Links come only from the authenticated ``/app/release`` and expire after a day.
"""

from __future__ import annotations

import time
from functools import cache

from core import signing

_LIFETIME_SECONDS = 86400


@cache
def _secret() -> bytes:
    return signing.resolve_secret("APP_LINK_SECRET")


def _payload(name: str, expires: int) -> str:
    return f"app-download:{name}:{expires}"


def signed_path(name: str) -> str:
    expires = int(time.time()) + _LIFETIME_SECONDS
    signature = signing.sign(_secret(), _payload(name, expires))
    return f"/app/download/{name}?e={expires}&s={signature}"


def verify(name: str, expires: int, signature: str) -> bool:
    if expires < time.time():
        return False
    return signing.verify(_secret(), _payload(name, expires), signature)
