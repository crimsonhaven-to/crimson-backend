"""Configuration, login and authenticated API calls for the operator's Jellyfin.

The access token lives only in this process: the resolver and the proxy inject
it server side, so it never reaches a browser. These calls use a plain client,
not ``guarded_client``, because ``JELLYFIN_URL`` is set by the operator and is
usually a LAN host that the SSRF guard would rightly refuse.
"""

import asyncio
import logging
from typing import Optional, Tuple

import httpx

from core.config import get_settings
from core.http_client import http_client

logger = logging.getLogger(__name__)

# Jellyfin ties the session and its transcodes to this identity, so it must stay
# stable across the master, variant and segment requests of one playback.
_CLIENT = "Crimson"
_DEVICE = "Crimson Backend"
_DEVICE_ID = "crimson-backend"
_VERSION = "2.0"

_token: Optional[str] = None
_user_id: Optional[str] = None
_auth_lock = asyncio.Lock()


def get_config() -> Tuple[str, str, str]:
    settings = get_settings()
    return settings.jellyfin_url, settings.jellyfin_username, settings.jellyfin_password or ""


def is_configured() -> bool:
    url, user, _ = get_config()
    return bool(url and user)


def auth_header(token: Optional[str] = None) -> str:
    parts = [
        f'MediaBrowser Client="{_CLIENT}"',
        f'Device="{_DEVICE}"',
        f'DeviceId="{_DEVICE_ID}"',
        f'Version="{_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return ", ".join(parts)


async def _authenticate() -> Tuple[str, str]:
    global _token, _user_id
    url, user, pw = get_config()
    if not (url and user):
        raise RuntimeError("Jellyfin not configured")
    async with http_client() as client:
        resp = await client.post(
            f"{url}/Users/AuthenticateByName",
            headers={"Authorization": auth_header(), "Content-Type": "application/json"},
            json={"Username": user, "Pw": pw},
            timeout=20.0,
        )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("AccessToken")
    user_id = (data.get("User") or {}).get("Id")
    if not token or not user_id:
        raise RuntimeError("Jellyfin auth returned no token / user id")
    _token, _user_id = token, user_id
    logger.info("Jellyfin: authenticated as %r (user %s)", user, user_id)
    return token, user_id


async def _ensure_auth() -> Tuple[str, str]:
    if _token and _user_id:
        return _token, _user_id
    async with _auth_lock:
        if _token and _user_id:
            return _token, _user_id
        return await _authenticate()


async def reauth(rejected_token: str) -> Tuple[str, str]:
    """A fresh login after ``rejected_token`` got a 401. Concurrent callers that
    hit the same 401 share one login instead of each starting their own."""
    async with _auth_lock:
        if _token and _user_id and _token != rejected_token:
            return _token, _user_id
        return await _authenticate()


async def api_request(
    method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None
) -> httpx.Response:
    """An authenticated Jellyfin API call that logs in again once on a 401."""
    url, _, _ = get_config()
    token, _uid = await _ensure_auth()

    async def send(tok: str) -> httpx.Response:
        async with http_client() as client:
            return await client.request(
                method,
                f"{url}{path}",
                headers={"Authorization": auth_header(tok), "Content-Type": "application/json"},
                params=params,
                json=json_body,
                timeout=30.0,
                follow_redirects=True,
            )

    resp = await send(token)
    if resp.status_code == 401:
        token, _uid = await reauth(token)
        resp = await send(token)
    resp.raise_for_status()
    return resp


async def api_get(path: str, params: Optional[dict] = None) -> dict:
    return (await api_request("GET", path, params=params)).json()
