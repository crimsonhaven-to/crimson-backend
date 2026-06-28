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
# the resolvers that call ``proxy_url``.
#
# NOTE: VOE is deliberately NOT here. Its CDN token is bound to the IP/ASN that
# resolved the embed (the ``asn=`` query param), so only the backend — the ASN
# the token was minted for — can fetch its segments. An external proxy on any
# other network (e.g. a Cloudflare Worker) gets a 403, exactly as the viewer's
# browser would. VOE therefore MUST stay on its same-origin /voe_proxy. The
# offloadable sources are the purely Referer/Origin-gated ones below.
ROUTED_SOURCES = ["cinema.bz", "PlayIMDb"]


def proxy_bases() -> list[str]:
    """Configured proxy origins (comma-separated), trailing slashes stripped."""
    return [
        b.strip().rstrip("/")
        for b in os.getenv("CRIMSON_PROXY_BASE", "").split(",")
        if b.strip()
    ]


def _source_allowlist() -> list[str]:
    """Optional per-source A/B allowlist (``CRIMSON_PROXY_SOURCES``). Empty/unset
    means *all* wired sources offload; set it to a comma-separated subset (e.g.
    ``cinema.bz,PlayIMDb``) to offload only those and keep the rest same-origin."""
    return [s.strip() for s in os.getenv("CRIMSON_PROXY_SOURCES", "").split(",") if s.strip()]


def _secret() -> bytes:
    """The shared signing secret — specifically ``PROXY_SECRET`` (the value the
    edge proxy carries as ``NITRO_PROXY_SECRET``), never a per-source secret."""
    return (os.getenv("PROXY_SECRET") or "").encode("utf-8")


def is_enabled(source: str | None = None) -> bool:
    """True only when we have at least one proxy host AND a secret to sign with.

    When ``source`` is given, also honour the optional ``CRIMSON_PROXY_SOURCES``
    allowlist so individual sources can be A/B'd on/off without code changes
    (unset allowlist = every wired source offloads). Call with no ``source`` for
    the global "is the proxy configured at all" check (used by the dashboard)."""
    if not (proxy_bases() and os.getenv("PROXY_SECRET")):
        return False
    if source is not None:
        allow = _source_allowlist()
        if allow and source not in allow:
            return False
    return True


def _signed_query(url: str, referer: str, origin: str, user_agent: str) -> str:
    """The ``u=…&r=…&o=…&ua=…&s=…`` query string, signed per the crimson-proxy
    contract (HMAC over ``url\\nreferer\\norigin\\nuser-agent``, hex[:32])."""
    payload = "\n".join([url, referer, origin, user_agent])
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return (
        f"u={quote(url, safe='')}"
        f"&r={quote(referer, safe='')}"
        f"&o={quote(origin, safe='')}"
        f"&ua={quote(user_agent, safe='')}"
        f"&s={sig}"
    )


def proxy_url(url: str, *, referer: str = "", origin: str = "", user_agent: str = "") -> str:
    """Build a signed link to the external proxy for ``url`` with the upstream
    headers the gated CDN requires. Picks one configured host at random (all
    share the secret, so the link is valid on any of them)."""
    return f"{random.choice(proxy_bases())}/?{_signed_query(url, referer, origin, user_agent)}"


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

    have_secret = bool(os.getenv("PROXY_SECRET"))
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
