"""SSRF guard classification + redirect-hop enforcement.

This is the control that stops a hostile upstream redirect from turning the
stream proxies into an SSRF primitive (cloud metadata / internal hosts). The
address classifier is the security boundary, so it gets exhaustive coverage.
"""

import pytest

from resolvers._ssrf_guard import (
    SSRFError,
    _addr_is_blocked,
    _assert_public_host,
    guarded_client,
)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",        # loopback
        "10.0.0.1",         # private A
        "172.16.5.4",       # private B
        "192.168.1.1",      # private C
        "169.254.169.254",  # link-local — the AWS/GCP metadata endpoint
        "0.0.0.0",          # unspecified
        "224.0.0.1",        # multicast
        "::1",              # IPv6 loopback
        "fe80::1",          # IPv6 link-local
        "fd00::1",          # IPv6 unique-local (private)
        "not-an-ip",        # unparseable -> refuse
    ],
)
def test_blocked_addresses(ip):
    assert _addr_is_blocked(ip) is True


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_allowed(ip):
    assert _addr_is_blocked(ip) is False


def test_ipv6_zone_id_is_stripped():
    # A scoped IPv6 literal must still classify (the zone id is not part of the
    # address) — otherwise it'd be treated as unparseable and wrongly refused.
    assert _addr_is_blocked("fe80::1%eth0") is True


async def test_assert_public_host_rejects_literal_private_ip():
    with pytest.raises(SSRFError):
        await _assert_public_host("169.254.169.254")


async def test_assert_public_host_rejects_empty():
    with pytest.raises(SSRFError):
        await _assert_public_host(None)


async def test_assert_public_host_allows_public_literal():
    # Literal public IP: no DNS, must pass without raising.
    await _assert_public_host("1.1.1.1")


async def test_assert_public_host_rejects_unresolvable():
    with pytest.raises(SSRFError):
        await _assert_public_host("nonexistent.invalid.crimson-test.example")


def test_guarded_client_is_async_client():
    import httpx

    client = guarded_client()
    assert isinstance(client, httpx.AsyncClient)
