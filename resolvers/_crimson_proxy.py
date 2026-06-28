"""External CORS proxy (crimson-proxy) URL builder + health probe.

Phase 1 of moving stream-segment bandwidth off the backend: when
``CRIMSON_PROXY_BASE`` is set, the simple *static-header HLS* sources (VOE,
cinema.bz, PlayIMDb) hand the player a signed link to the external edge proxy
(Netlify / Cloudflare Workers) instead of a same-origin ``/{source}_proxy``
path. Segment bytes then flow ``CDN → edge proxy → viewer`` and never touch us.

The signature contract is **byte-for-byte** the crimson-proxy one (see that
repo's README): HMAC-SHA256 over ``url\\nreferer\\norigin\\nuser-agent``, hex
truncated to 32 chars. The proxy holds the *same* secret (``NITRO_PROXY_SECRET``
== our ``PROXY_SECRET``), so it can re-sign the playlist's child segments itself
— we only ever sign the top-level stream URL.

Because the signature covers the query fields and **not** the host, one signed
link is valid on every proxy that shares the secret. So ``CRIMSON_PROXY_BASE``
may be a comma-separated list and we pick one host per request — free
round-robin load-balancing / failover across the free tiers.

Gating: this is OFF unless BOTH ``CRIMSON_PROXY_BASE`` and ``PROXY_SECRET`` are
set. Leave either unset and every source keeps proxying itself (same-origin
``/{source}_proxy``), so this is a safe, flag-gated, A/B-per-source swap. A blank
secret would mean the proxy is in open mode, which we never sign for.
"""

import hashlib
import hmac
import logging
import os
import random
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Display labels of the sources wired to prefer the external proxy when enabled.
# Used by the admin dashboard to show what's being offloaded; kept in sync with
# the resolvers that call ``proxy_url`` (voe / cinemabz / playimdb).
ROUTED_SOURCES = ["Voe", "cinema.bz", "PlayIMDb"]


def proxy_bases() -> list[str]:
    """Configured proxy origins (comma-separated), trailing slashes stripped."""
    return [
        b.strip().rstrip("/")
        for b in os.getenv("CRIMSON_PROXY_BASE", "").split(",")
        if b.strip()
    ]


def _secret() -> bytes:
    """The shared signing secret — specifically ``PROXY_SECRET`` (the value the
    edge proxy carries as ``NITRO_PROXY_SECRET``), never a per-source secret."""
    return (os.getenv("PROXY_SECRET") or "").encode("utf-8")


def is_enabled() -> bool:
    """True only when we have at least one proxy host AND a secret to sign with."""
    return bool(proxy_bases()) and bool(os.getenv("PROXY_SECRET"))


def proxy_url(url: str, *, referer: str = "", origin: str = "", user_agent: str = "") -> str:
    """Build a signed link to the external proxy for ``url`` with the upstream
    headers the gated CDN requires. Picks one configured host at random (all
    share the secret, so the link is valid on any of them)."""
    payload = "\n".join([url, referer, origin, user_agent])
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    q = (
        f"u={quote(url, safe='')}"
        f"&r={quote(referer, safe='')}"
        f"&o={quote(origin, safe='')}"
        f"&ua={quote(user_agent, safe='')}"
        f"&s={sig}"
    )
    return f"{random.choice(proxy_bases())}/?{q}"


async def probe_bases(timeout: float = 4.0) -> list[dict]:
    """Health-ping each configured proxy host's ``GET /`` (the proxy's own
    health check) so the admin dashboard can show which CORS proxies are live.
    Returns one ``{base, status, ok, detail}`` dict per host."""
    bases = proxy_bases()
    if not bases:
        return []

    results: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for base in bases:
            entry = {"base": base, "status": "error", "code": None, "detail": ""}
            try:
                resp = await client.get(f"{base}/")
                entry["code"] = resp.status_code
                if resp.status_code == 200:
                    entry["status"] = "active"
                    # The proxy answers its health check with a small JSON body;
                    # surface a hint of it if present, else just note it's up.
                    try:
                        body = resp.json()
                        entry["detail"] = body.get("name") or body.get("status") or "up"
                    except Exception:
                        entry["detail"] = "up"
                else:
                    entry["detail"] = f"HTTP {resp.status_code}"
            except Exception as exc:  # network/DNS/timeout -> host is down
                entry["detail"] = type(exc).__name__
            results.append(entry)
    return results
