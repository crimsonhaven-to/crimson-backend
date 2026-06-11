"""
SSRF guard for the third-party stream proxies (PlayIMDb / AnimeSuge / Movish).

Those proxies fetch *untrusted* upstreams: the signed proxies hit rotating CDN
hosts, and even the host-allow-listed ones (Movish) follow redirects.
httpx's signature/allow-list check only validates the *initial* URL — once a 3xx
arrives, httpx will happily follow it to **any** host. A malicious or compromised
upstream could therefore redirect the backend at ``http://169.254.169.254/`` (cloud
metadata) or an internal service, turning the proxy into an SSRF primitive whose
response streams straight back to the caller.

The fix: a custom transport that validates the target host on **every** request —
including each redirect hop, since the client re-enters the transport per hop — and
refuses any host that resolves to a private / loopback / link-local / reserved
address. ``guarded_client`` is a drop-in for ``httpx.AsyncClient`` (same kwargs).

NOT used by the Jellyfin source: its ``JELLYFIN_URL`` is operator-configured and
*intentionally* a private/LAN host, so the same rule would (correctly, for the
generic case) refuse it. Jellyfin is a trusted, fixed host — not a redirect-driven
SSRF vector — so it keeps the plain client.

Residual caveat: a determined attacker could still attempt DNS-rebinding in the
narrow window between this lookup and httpx's own connect-time resolution. Pinning
the validated IP would close that; for this threat model (blocking metadata /
internal-host SSRF via a hostile upstream redirect) the resolve-and-check here is
the standard, high-value mitigation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


class SSRFError(ValueError):
    """Raised when a request targets a non-public address. Subclasses
    ``ValueError`` so the api.py proxy routes map it to a 403 like the existing
    signature/allow-list rejections."""


def _addr_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])  # strip IPv6 zone id
    except ValueError:
        return True  # unparseable -> refuse
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

    # Literal IP — check directly, no DNS.
    try:
        ipaddress.ip_address(host)
        if _addr_is_blocked(host):
            raise SSRFError(f"Blocked non-public address: {host}")
        return
    except ValueError:
        pass  # it's a hostname; resolve it

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFError(f"Cannot resolve host: {host}") from e
    for info in infos:
        ip = info[4][0]
        if _addr_is_blocked(ip):
            raise SSRFError(f"{host} resolves to a blocked address ({ip})")


class _GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """Validates the destination host before every connection. The client calls
    the transport once per request *and once per redirect hop*, so this guards
    the whole redirect chain, not just the first URL."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await _assert_public_host(request.url.host)
        return await super().handle_async_request(request)


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` that refuses any request (incl. redirect hops) whose
    host resolves to a private/loopback/link-local/reserved address. Accepts the
    same kwargs as ``httpx.AsyncClient`` (headers, timeout, follow_redirects, …)."""
    return httpx.AsyncClient(transport=_GuardedAsyncTransport(), **kwargs)
