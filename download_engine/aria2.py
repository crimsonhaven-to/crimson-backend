"""
Thin async JSON-RPC client for the aria2c sidecar.

aria2 does the actual byte-pulling for both plain http/https URLs and ``magnet:``
links (and .torrent URLs), exposing a JSON-RPC 2.0 endpoint. We talk to it over
the internal Docker network — never the public reverse proxy — authenticated with
a shared secret token (``ARIA2_RPC_SECRET``). The manager owns all polling; this
module is just the transport + the few methods we use.

Config (env):
  * ``ARIA2_RPC_URL``     — e.g. ``http://aria2:6800/jsonrpc`` (default).
  * ``ARIA2_RPC_SECRET``  — the ``--rpc-secret`` aria2 is started with. Must match.

Leech-only: downloads are added with ``seed-time=0`` so a completed torrent stops
seeding immediately (no inbound port needed), and each job runs in its own staging
``dir`` so aria2's ``.aria2`` control files stay isolated per job.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx
from core.config import get_settings

logger = logging.getLogger("download_engine.aria2")

_TIMEOUT = 15.0

# Status fields we ask aria2 for (keeps responses small).
_STATUS_KEYS = [
    "gid", "status", "totalLength", "completedLength", "downloadSpeed",
    "errorCode", "errorMessage", "followedBy", "files", "dir",
]


class Aria2Error(RuntimeError):
    """An aria2 JSON-RPC call returned an error object or was unreachable."""


def _token() -> str:
    return f"token:{get_settings().aria2_rpc_secret}"


async def _call(method: str, params: Optional[list] = None, *, timeout: Optional[float] = None):
    """Issue one JSON-RPC call, returning ``result`` or raising ``Aria2Error``."""
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


# --- capability probe -------------------------------------------------------
async def is_available() -> bool:
    """True when the aria2 sidecar answers (used for the dashboard availability
    badge and to gate submission)."""
    try:
        # Short timeout: this runs on the hot dashboard path, so a hung sidecar must
        # not stall it (a down sidecar refuses instantly anyway).
        await _call("aria2.getVersion", timeout=3.0)
        return True
    except Aria2Error:
        return False


# --- lifecycle --------------------------------------------------------------
async def add_uri(uri: str, staging_dir: str) -> str:
    """Queue a download of ``uri`` (http/https or magnet) into ``staging_dir`` and
    return the aria2 gid. Torrents are leech-only: aria2 stops once complete."""
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
    """Status dict for a gid (see ``_STATUS_KEYS``). Raises ``Aria2Error`` when aria2
    no longer knows the gid (e.g. after an aria2 restart)."""
    result = await _call("aria2.tellStatus", [gid, _STATUS_KEYS])
    return result if isinstance(result, dict) else {}


async def pause(gid: str) -> None:
    try:
        await _call("aria2.pause", [gid])
    except Aria2Error as e:
        logger.debug(f"aria2 pause({gid}) failed: {e}")


async def unpause(gid: str) -> None:
    """Raises ``Aria2Error`` when aria2 no longer knows the gid, so the caller can
    requeue the job instead."""
    await _call("aria2.unpause", [gid])


async def remove(gid: str) -> None:
    """Stop and forget a download, best effort, then drop its result row so the
    gid fully disappears."""
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
    """A magnet first downloads *metadata*; once it resolves, aria2 spawns a new gid
    for the actual data and lists it in ``followedBy``. Return that gid so the manager
    tracks the real download, or None when there's no hand-off."""
    followed: List[str] = status.get("followedBy") or []
    return followed[0] if followed else None
