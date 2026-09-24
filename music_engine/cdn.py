"""The optional off-site copy of the library: the music-cdn Worker in front of
an R2 bucket (deploy/music-cdn).

R2's own pre-signed URLs only work on its S3 endpoint, never on a custom
domain, so the Worker checks our signature instead and streams from the
bucket. Uploads go through the same Worker with the shared secret as a bearer
token, which keeps S3 credentials and an S3 client out of the backend.

A bucket object's key is the file's path relative to the share, so the bucket
is a browsable copy of the library that ``rclone`` can restore from.
"""

from __future__ import annotations

import os
import time
from urllib.parse import quote

import httpx

from core import signing
from core.config import get_settings

from . import links

UPLOAD_TIMEOUT = 300.0


class CdnError(Exception):
    pass


def enabled() -> bool:
    settings = get_settings()
    return bool(settings.music_cdn_url and settings.music_cdn_secret)


def object_key(rel_path: str) -> str:
    return rel_path.replace(os.sep, "/")


def _payload(key: str, expires: int) -> str:
    return f"music-cdn:{key}:{expires}"


def signed_url(rel_path: str) -> str:
    """Same lifetime as the backend's own links, so revoking access works the same."""
    settings = get_settings()
    key = object_key(rel_path)
    expires = links.expiry()
    signature = signing.sign(settings.music_cdn_secret.encode("utf-8"), _payload(key, expires))
    return f"{settings.music_cdn_url}/{quote(key)}?e={expires}&s={signature}"


def verify(key: str, expires: int, signature: str) -> bool:
    """The Worker's check, mirrored here so a test pins both to one scheme."""
    if expires < time.time():
        return False
    secret = get_settings().music_cdn_secret.encode("utf-8")
    return signing.verify(secret, _payload(key, expires), signature)


async def upload(path: str, rel_path: str, content_type: str) -> None:
    settings = get_settings()
    with open(path, "rb") as handle:
        body = handle.read()
    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            response = await client.put(
                f"{settings.music_cdn_url}/{quote(object_key(rel_path))}",
                content=body,
                headers={
                    "Authorization": f"Bearer {settings.music_cdn_secret}",
                    "Content-Type": content_type,
                },
            )
    except httpx.HTTPError as e:
        raise CdnError(f"upload of {rel_path} failed: {e}") from e
    if response.status_code not in (200, 201):
        raise CdnError(f"upload of {rel_path} failed: HTTP {response.status_code}")
