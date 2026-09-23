"""Signed links to the external crimson-proxy edge, and its health probe.

Powers ``/sign`` and the dashboard's proxy-health ping.
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
from typing import Any
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


# Routing skips a host the last probe saw down. Probes run from the scheduler and
# the dashboard; an entry older than the TTL is ignored, and with no host known
# healthy every base is a candidate, so a cold cache is no worse than no cache.
_health: dict[str, dict] = {}
_HEALTH_TTL = 300.0


def _is_known_healthy(base: str, now: float) -> bool:
    entry = _health.get(base)
    return bool(entry and (now - entry["ts"]) <= _HEALTH_TTL and entry["healthy"])


def _candidate_bases() -> list[str]:
    bases = proxy_bases()
    now = time.time()
    return [b for b in bases if _is_known_healthy(b, now)] or bases


def proxy_url(url: str, *, referer: str = "", origin: str = "", user_agent: str = "") -> str | None:
    """A signed proxy link for ``url`` carrying the headers the upstream CDN
    wants, on a random healthy base. None when no base is configured."""
    bases = _candidate_bases()
    if not bases:
        return None
    return f"{random.choice(bases)}/?{_signed_query(url, referer, origin, user_agent)}"


# The proxy checks the signature before fetching, so this URL never has to exist:
# a 401 means our secret is wrong, anything else means it matches.
_CANARY_URL = "https://example.com/crimson-proxy-probe.m3u8"


async def probe_bases(timeout: float = 5.0) -> list[dict]:
    """One ``{base, status, code, signed, secret_ok, detail}`` per configured host.

    ``GET /`` gives liveness and whether the host enforces signing; a signed
    canary then tells a matching secret from the classic mismatch that 401s every
    stream. ``secret_ok`` is None when it could not be determined."""
    bases = proxy_bases()
    if not bases:
        return []

    have_secret = bool(get_settings().proxy_secret)
    canary_q = _signed_query(_CANARY_URL, "", "", "") if have_secret else ""

    results: list[dict] = []
    # Runs on the scheduler thread's own event loop, so not the shared client.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for base in bases:
            entry: dict[str, Any] = {
                "base": base, "status": "error", "code": None, "signed": None, "secret_ok": None, "detail": "",
            }
            try:
                resp = await client.get(f"{base}/")
            except Exception as exc:
                entry["detail"] = type(exc).__name__
                results.append(entry)
                continue
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

            if have_secret and entry["status"] == "active":
                try:
                    cresp = await client.get(f"{base}/?{canary_q}")
                    if cresp.status_code == 401:
                        entry.update(secret_ok=False, status="error", detail="secret mismatch (401)")
                    elif entry["signed"] is False:
                        # Signed links still work, but a host with no secret is an open relay.
                        entry.update(secret_ok=None, status="idle", detail="open mode: NITRO_PROXY_SECRET unset")
                    else:
                        entry.update(secret_ok=True, detail="signed OK")
                except Exception as exc:
                    entry["detail"] = f"canary: {type(exc).__name__}"
            results.append(entry)
    return results


async def refresh_health(timeout: float = 5.0) -> list[dict]:
    """Probe every host, update the routing cache and return the probe results.

    ``active`` and ``idle`` (open mode, signature ignored) both honour our links;
    ``error`` covers down hosts and a secret mismatch that would reject them all."""
    results = await probe_bases(timeout=timeout)
    now = time.time()
    for r in results:
        _health[r["base"]] = {"healthy": r["status"] in ("active", "idle"), "ts": now}
    return results
