"""The release files, read from wherever CI uploads them (a private GitLab
generic package on the official instance). The backend is the only reader, so
the files stay members-only while the repository itself is public.

Upstream is ``APP_RELEASES_URL``; ``APP_RELEASES_TOKEN`` goes along as a bearer.
httpx drops Authorization on a redirect to another origin, so following GitLab's
redirect to object storage never leaks it.
"""

import re
import time
from typing import AsyncIterator, Dict, Optional, Tuple

import httpx

from core.config import get_settings

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MANIFEST = "release.json"
_MANIFEST_TTL = 300.0
_TIMEOUT = httpx.Timeout(30.0, read=120.0)

# Replaced in tests with an httpx.MockTransport.
transport: Optional[httpx.AsyncBaseTransport] = None


class NotFound(Exception):
    pass


class UpstreamError(Exception):
    pass


def configured() -> bool:
    return bool(get_settings().app_releases_url)


def valid_name(name: str) -> bool:
    return bool(NAME.match(name))


def _client() -> httpx.AsyncClient:
    token = get_settings().app_releases_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, headers=headers, transport=transport)


def _url(name: str) -> str:
    return f"{get_settings().app_releases_url.rstrip('/')}/{name}"


_manifest: Tuple[float, Optional[Dict]] = (0.0, None)


async def manifest() -> Dict:
    """Cached per replica: every member's Settings page asks for it."""
    global _manifest
    fetched_at, cached = _manifest
    if cached is not None and time.monotonic() - fetched_at < _MANIFEST_TTL:
        return cached
    try:
        async with _client() as client:
            response = await client.get(_url(MANIFEST))
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise UpstreamError(str(e)) from e
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise UpstreamError("release.json has no file list")
    _manifest = (time.monotonic(), data)
    return data


def forget_manifest() -> None:
    global _manifest
    _manifest = (0.0, None)


async def open_file(name: str) -> Tuple[Dict[str, str], AsyncIterator[bytes]]:
    """Headers worth forwarding and the body, streamed so a 100 MB installer is
    never held in memory. The client closes once the body is consumed."""
    client = _client()
    try:
        response = await client.send(client.build_request("GET", _url(name)), stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        raise UpstreamError(str(e)) from e
    if response.status_code != 200:
        await response.aclose()
        await client.aclose()
        if response.status_code == 404:
            raise NotFound(name)
        raise UpstreamError(f"upstream answered {response.status_code}")

    headers = {}
    if "content-length" in response.headers:
        headers["Content-Length"] = response.headers["content-length"]

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return headers, body()
