"""JSON-RPC client for the aria2c sidecar, reached over the internal Docker network
with the shared ``ARIA2_RPC_SECRET``.

Jobs are leech-only (``seed-time=0``), so no inbound port is needed, and each runs
in its own staging dir so aria2's ``.aria2`` control files stay per job.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx
from core.config import get_settings

logger = logging.getLogger("download_engine.aria2")

_TIMEOUT = 15.0

_STATUS_KEYS = [
    "gid", "status", "totalLength", "completedLength", "downloadSpeed",
    "errorCode", "errorMessage", "followedBy", "files", "dir",
]


class Aria2Error(RuntimeError):
    pass


def _token() -> str:
    return f"token:{get_settings().aria2_rpc_secret}"


async def _call(method: str, params: Optional[list] = None, *, timeout: Optional[float] = None):
    payload = {
        "jsonrpc": "2.0",
        "id": "crimson",
        "method": method,
        "params": [_token(), *(params or [])],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout or _TIMEOUT) as client:
            resp = await client.post(get_settings().aria2_rpc_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise Aria2Error(f"aria2 RPC {method} transport error: {e}") from e
    if isinstance(data, dict) and data.get("error"):
        raise Aria2Error(f"aria2 RPC {method} error: {data['error']}")
    return data.get("result") if isinstance(data, dict) else None


async def is_available() -> bool:
    try:
        # The dashboard calls this, so a hung sidecar must not stall it.
        await _call("aria2.getVersion", timeout=3.0)
        return True
    except Aria2Error:
        return False


async def add_uri(uri: str, staging_dir: str) -> str:
    """The gid of a new download of an http(s) URL or magnet into ``staging_dir``."""
    options = {
        "dir": staging_dir,
        # Resume a partial from a previous run's control file instead of restarting.
        "continue": "true",
        "auto-file-renaming": "false",
        "seed-time": "0",
        "bt-remove-unselected-file": "true",
    }
    gid = await _call("aria2.addUri", [[uri], options])
    if not isinstance(gid, str):
        raise Aria2Error(f"aria2.addUri returned an unexpected gid: {gid!r}")
    return gid


async def tell_status(gid: str) -> dict:
    """Raises ``Aria2Error`` when aria2 no longer knows the gid, as after a restart."""
    result = await _call("aria2.tellStatus", [gid, _STATUS_KEYS])
    return result if isinstance(result, dict) else {}


async def pause(gid: str) -> None:
    try:
        await _call("aria2.pause", [gid])
    except Aria2Error as e:
        logger.debug(f"aria2 pause({gid}) failed: {e}")


async def unpause(gid: str) -> None:
    """Raises ``Aria2Error`` when aria2 no longer knows the gid, so the caller can
    requeue instead."""
    await _call("aria2.unpause", [gid])


async def remove(gid: str) -> None:
    """Best effort. The result row is dropped too, so the gid fully disappears."""
    for method in ("aria2.forceRemove", "aria2.remove"):
        try:
            await _call(method, [gid])
            break
        except Aria2Error:
            continue
    try:
        await _call("aria2.removeDownloadResult", [gid])
    except Aria2Error:
        pass


def followed_gid(status: dict) -> Optional[str]:
    """The data download's gid once a magnet's metadata has resolved, or None."""
    followed: List[str] = status.get("followedBy") or []
    return followed[0] if followed else None
