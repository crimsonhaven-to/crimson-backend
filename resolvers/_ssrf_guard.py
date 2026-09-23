"""An httpx client that refuses private, loopback and reserved destinations.

The overlay's stream proxies fetch untrusted upstreams and follow redirects, but
httpx only lets the caller check the first URL. A hostile upstream could redirect
the backend to ``169.254.169.254`` (cloud metadata) or an internal service and
have the response streamed back to the caller. The transport here checks the
host on every request, and the client re-enters the transport per redirect hop,
so the whole chain is covered.

DNS rebinding between this lookup and httpx's own connect-time resolution is not
covered. Pinning the checked IP would close it; against a redirecting upstream,
resolve-and-check is the mitigation that matters.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


class SSRFError(ValueError):
    """A ValueError so the proxy routes answer 403, as for a bad signature."""


def _addr_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])  # IPv6 zone id
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def _assert_public_host(host: str | None) -> None:
    if not host:
        raise SSRFError("Request has no host")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _addr_is_blocked(host):
            raise SSRFError(f"Blocked non-public address: {host}")
        return

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFError(f"Cannot resolve host: {host}") from e
    for info in infos:
        ip = str(info[4][0])
        if _addr_is_blocked(ip):
            raise SSRFError(f"{host} resolves to a blocked address ({ip})")


class _GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await _assert_public_host(request.url.host)
        return await super().handle_async_request(request)


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """A drop-in ``httpx.AsyncClient`` (same kwargs) that refuses non-public hosts."""
    return httpx.AsyncClient(transport=_GuardedAsyncTransport(), **kwargs)
