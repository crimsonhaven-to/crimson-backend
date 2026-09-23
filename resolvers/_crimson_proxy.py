"""Signed links to the external crimson-proxy edge, and its health probe.

Powers ``/sign``, the cache downloader and the dashboard's proxy-health ping.
The signature is byte-for-byte the crimson-proxy contract: HMAC-SHA256 over
``url\nreferer\norigin\nuser-agent``, hex truncated to 32 characters, keyed with
``PROXY_SECRET`` (the proxy's ``NITRO_PROXY_SECRET``). It covers the query and not
the host, so one link is valid on every configured base, which is what lets
``CRIMSON_PROXY_BASE`` list several hosts for failover.

Off unless both ``CRIMSON_PROXY_BASE`` and ``PROXY_SECRET`` are set: a blank
secret would mean the proxy runs in open mode, which is never signed for.
"""

import logging
import random
import time
from urllib.parse import quote

import httpx

from core import signing
from core.config import get_settings

logger = logging.getLogger(__name__)


def proxy_bases() -> list[str]:
    return get_settings().crimson_proxy_base


def is_enabled() -> bool:
    return bool(proxy_bases() and get_settings().proxy_secret)


def _signed_query(url: str, referer: str, origin: str, user_agent: str) -> str:
    payload = "\n".join([url, referer, origin, user_agent])
    sig = signing.sign(get_settings().proxy_secret.encode("utf-8"), payload)
    return (
        f"u={quote(url, safe='')}"
        f"&r={quote(referer, safe='')}"
        f"&o={quote(origin, safe='')}"
        f"&ua={quote(user_agent, safe='')}"
        f"&s={sig}"
    )


# --- health-aware host selection (automatic failover) ----------------------
# The signed query is host-independent (it covers url/referer/origin/ua, NOT the
# host), so one signature is valid on every base that shares the secret. That lets
# us route AWAY from a host that's down without re-signing: we keep a small health
# cache, refreshed by the scheduler + the admin dashboard probe, and pick only from
# the hosts last seen up. So if (say) the Netlify edge is 404ing, every link goes to
# the Cloudflare worker automatically, and vice-versa.
#
# Cold/stale/all-down cache => fall back to ALL configured bases, so we're never
# worse than the old plain random.choice. Reads/writes are dict-atomic under the
# GIL; a slightly-stale read at worst picks a host that just went down, which then
# fails the one fetch and is dropped on the next refresh — no lock needed.
_health: dict[str, dict] = {}      # base -> {"healthy": bool, "ts": float}
_HEALTH_TTL = 300.0                # a probe result older than this is ignored


def _is_known_healthy(base: str, now: float) -> bool:
    entry = _health.get(base)
    return bool(entry and (now - entry["ts"]) <= _HEALTH_TTL and entry["healthy"])


def _candidate_bases() -> list[str]:
    """Configured bases filtered to those last probed healthy; if none are known
    healthy (cold cache / all stale / genuinely all down) returns every base, so
    routing degrades to "try anything" rather than giving up."""
    bases = proxy_bases()
    if not bases:
        return []
    now = time.time()
    healthy = [b for b in bases if _is_known_healthy(b, now)]
    return healthy or bases


def proxy_url(url: str, *, referer: str = "", origin: str = "", user_agent: str = "") -> str:
    """Build a signed link to the external proxy for ``url`` with the upstream
    headers the gated CDN requires. Picks one *healthy* configured host at random
    (all share the secret, so the link is valid on any of them); a host that the
    last probe saw down is skipped — automatic failover to the survivors."""
    return f"{random.choice(_candidate_bases())}/?{_signed_query(url, referer, origin, user_agent)}"


# A harmless URL the secret-match canary points at. The proxy verifies the
# signature BEFORE fetching, so what matters is 401 (bad secret) vs anything
# else (secret OK) — the URL itself never needs to resolve to a real stream.
_CANARY_URL = "https://example.com/crimson-proxy-probe.m3u8"


async def probe_bases(timeout: float = 5.0) -> list[dict]:
    """Probe each configured proxy host for the admin dashboard. Per host:

      * ``GET /``  — liveness + whether it enforces signing (``signed`` flag).
      * a signed **canary** request — the proxy verifies the signature before
        fetching, so a 401 means its secret does NOT match ours (the classic
        "all streams 401 / stuck at 00:00" cause); anything else means the
        secret matches (or the host is in open mode).

    Returns one ``{base, status, code, signed, secret_ok, detail}`` per host.
    ``secret_ok`` is True/False, or None when it couldn't be determined."""
    bases = proxy_bases()
    if not bases:
        return []

    have_secret = bool(get_settings().proxy_secret)
    canary_q = _signed_query(_CANARY_URL, "", "", "") if have_secret else ""

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for base in bases:
            entry = {
                "base": base,
                "status": "error",
                "code": None,
                "signed": None,
                "secret_ok": None,
                "detail": "",
            }
            try:
                resp = await client.get(f"{base}/")
                entry["code"] = resp.status_code
                if resp.status_code == 200:
                    entry["status"] = "active"
                    entry["detail"] = "up"
                    try:
                        entry["signed"] = bool(resp.json().get("signed"))
                    except Exception:
                        pass
                else:
                    entry["detail"] = f"HTTP {resp.status_code}"
            except Exception as exc:  # network/DNS/timeout -> host is down
                entry["detail"] = type(exc).__name__
                results.append(entry)
                continue

            # Secret-match canary (only meaningful when WE have a secret to sign
            # with and the host is enforcing signing).
            if have_secret and entry["status"] == "active":
                try:
                    cresp = await client.get(f"{base}/?{canary_q}")
                    if cresp.status_code == 401:
                        entry["secret_ok"] = False
                        entry["status"] = "error"
                        entry["detail"] = "secret mismatch (401)"
                    elif entry["signed"] is False:
                        # Host accepted us but isn't enforcing signing at all —
                        # it has no secret set (open mode). Flag it: signed links
                        # work, but the proxy is abusable as an open relay.
                        entry["secret_ok"] = None
                        entry["status"] = "idle"
                        entry["detail"] = "open mode — NITRO_PROXY_SECRET unset"
                    else:
                        entry["secret_ok"] = True
                        entry["detail"] = "signed OK"
                except Exception as exc:
                    entry["detail"] = f"canary: {type(exc).__name__}"
            results.append(entry)
    return results


async def refresh_health(timeout: float = 5.0) -> list[dict]:
    """Probe every configured host and update the routing health cache, then return
    the probe results (same shape as ``probe_bases``) so callers can reuse them.

    A host is "healthy" for routing if it's reachable AND will honour our signed
    links — i.e. ``status`` is ``active`` (secret matches) or ``idle`` (open mode,
    signature ignored). ``error`` (down, DNS/timeout, or a 401 secret-mismatch that
    would reject every link) is unhealthy, so ``proxy_url`` stops routing to it.

    Called at startup, on a scheduler interval, and whenever the admin dashboard
    probes — so the cache reflects the same view the dashboard shows."""
    results = await probe_bases(timeout=timeout)
    now = time.time()
    for r in results:
        base = r.get("base")
        if not base:
            continue
        _health[base] = {"healthy": r.get("status") in ("active", "idle"), "ts": now}
    return results
