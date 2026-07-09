"""
IPTV service — a browsable catalogue of the world's free-to-air TV, courtesy of
the iptv-org project (https://github.com/iptv-org/iptv).

Nothing here is hosted, stored, or shipped by us: iptv-org curates a public,
daily-updated index of publicly available broadcast streams, and this service
fetches that index (the JSON API at https://iptv-org.github.io/api/), joins it
into a compact in-process catalogue, and serves it read-only. We honour the
project's own blocklist (channels removed on request of the rights holder) and
exclude NSFW-flagged channels by default.

Data joined per refresh (all fetched from IPTV_API_BASE):
  * ``channels.json``  — channel identity (id, name, country, categories, …)
  * ``streams.json``   — the playable HLS URLs (joined on channel id)
  * ``logos.json``     — channel logos (best one picked per channel)
  * ``categories.json``/``countries.json`` — the browse facets
  * ``blocklist.json`` — channels we must not surface

Playback goes through the signed same-origin ``/iptv_proxy`` (see routes):
roughly a fifth of the indexed streams are plain ``http://`` (mixed content —
an https page can't touch them), most serve no usable CORS, and some gate on a
Referer/User-Agent the viewer's browser can't send. The proxy solves all three
the same way the other operator proxies do.

Configuration (all optional):
  * ``IPTV_ENABLED``        — master switch (default true).
  * ``IPTV_REFRESH_HOURS``  — hours between catalogue refreshes (default 12;
                              upstream publishes daily).
  * ``IPTV_INCLUDE_NSFW``   — include NSFW-flagged channels (default false).

The catalogue lives per replica (no cross-replica coordination), exactly like
the changelog cache; a refresh is ~25 MB of JSON twice a day.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import quote, urljoin

import httpx

from resolvers._proxy_secret import resolve_secret
from resolvers._ssrf_guard import guarded_client

logger = logging.getLogger("crimson.iptv")

IPTV_API_BASE = "https://iptv-org.github.io/api"
PROXY_PREFIX = "/iptv_proxy"

# A stream with no quality tag sorts below any tagged one but above nothing.
_UNKNOWN_QUALITY = -1

# Sent upstream when a stream doesn't demand its own User-Agent.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def enabled() -> bool:
    """Master switch — IPTV needs no secrets, so it defaults on."""
    return (os.getenv("IPTV_ENABLED") or "true").strip().lower() not in ("0", "false", "no")


def _include_nsfw() -> bool:
    return (os.getenv("IPTV_INCLUDE_NSFW") or "").strip().lower() in ("1", "true", "yes")


def _refresh_hours() -> float:
    try:
        return max(1.0, float(os.getenv("IPTV_REFRESH_HOURS", "12")))
    except ValueError:
        return 12.0


# --- Signed proxy links ------------------------------------------------------
# Same shape as the other operator proxies: every upstream URL the player may
# ask /iptv_proxy to fetch is HMAC-signed, so the proxy is not an open relay.
# The signature covers the optional Referer/User-Agent too — otherwise a caller
# could replay a valid stream signature with attacker-chosen headers.

_secret: Optional[bytes] = None


def _proxy_secret() -> bytes:
    global _secret
    if _secret is None:
        _secret = resolve_secret("IPTV_PROXY_SECRET")
    return _secret


def sign_stream(url: str, referrer: str = "", user_agent: str = "") -> str:
    payload = "\n".join((url, referrer or "", user_agent or "")).encode("utf-8")
    return hmac.new(_proxy_secret(), payload, hashlib.sha256).hexdigest()[:32]


def verify_stream_sig(url: str, sig: str, referrer: str = "", user_agent: str = "") -> bool:
    if not sig:
        return False
    return hmac.compare_digest(sign_stream(url, referrer, user_agent), sig)


def proxy_path(url: str, referrer: str = "", user_agent: str = "") -> str:
    """Relative same-origin proxy path for one upstream URL (playlist or segment)."""
    parts = [
        f"{PROXY_PREFIX}?u={quote(url, safe='')}",
        f"s={sign_stream(url, referrer, user_agent)}",
    ]
    if referrer:
        parts.append(f"r={quote(referrer, safe='')}")
    if user_agent:
        parts.append(f"a={quote(user_agent, safe='')}")
    return "&".join(parts)


# --- HLS playlist rewriting ---------------------------------------------------
def rewrite_playlist(text: str, base_url: str, referrer: str = "", user_agent: str = "") -> str:
    """Rewrite an m3u8 so every sub-resource flows back through /iptv_proxy.

    Handles variant/segment lines and the ``URI="..."`` attribute inside
    EXT-X-MEDIA / EXT-X-KEY / EXT-X-MAP / EXT-X-I-FRAME-STREAM-INF tags.
    Relative URIs are resolved against ``base_url`` (the *final* upstream URL,
    after redirects) so the proxied link is always absolute + signed.
    """

    def _route(uri: str) -> str:
        uri = uri.strip()
        if not uri or uri.startswith("data:"):
            return uri
        return proxy_path(urljoin(base_url, uri), referrer, user_agent)

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(
                re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + _route(m.group(1)) + '"', line)
            )
        else:
            out.append(_route(stripped))
    return "\n".join(out)


def is_playlist(content_type: str, url: str) -> bool:
    if "mpegurl" in (content_type or "").lower():
        return True
    return url.split("?", 1)[0].lower().endswith((".m3u8", ".m3u"))


# --- Catalogue building (pure; unit-tested without network) -------------------
def _quality_rank(quality: Optional[str]) -> int:
    try:
        return int((quality or "").lower().rstrip("pi"))
    except ValueError:
        return _UNKNOWN_QUALITY


def build_catalog(
    channels: List[Dict],
    streams: List[Dict],
    categories: List[Dict],
    countries: List[Dict],
    logos: List[Dict],
    blocklist: List[Dict],
    include_nsfw: bool = False,
) -> Dict:
    """Join the raw iptv-org API payloads into the servable catalogue.

    Only channels that are alive (not closed/replaced), permitted (not on the
    project blocklist, not NSFW unless opted in) and actually *playable* (at
    least one stream) make it in.
    """
    blocked = {b.get("channel") for b in blocklist if b.get("channel")}

    # Best logo per channel: in_use first, feed-level logos only as fallback.
    logo_by_channel: Dict[str, Dict] = {}
    for lg in logos:
        ch = lg.get("channel")
        if not ch or not lg.get("url"):
            continue
        current = logo_by_channel.get(ch)
        score = (bool(lg.get("in_use")), lg.get("feed") is None, lg.get("width") or 0)
        if current is None or score > current["_score"]:
            logo_by_channel[ch] = {"url": lg["url"], "_score": score}

    streams_by_channel: Dict[str, List[Dict]] = {}
    for st in streams:
        ch = st.get("channel")
        url = st.get("url")
        if not ch or not url:
            continue
        streams_by_channel.setdefault(ch, []).append(
            {
                "url": url,
                "quality": st.get("quality"),
                "label": st.get("label"),
                "referrer": st.get("referrer") or "",
                "user_agent": st.get("user_agent") or "",
            }
        )

    country_names = {c["code"]: {"name": c.get("name") or c["code"], "flag": c.get("flag") or ""}
                     for c in countries if c.get("code")}
    category_names = {c["id"]: c.get("name") or c["id"] for c in categories if c.get("id")}

    records: Dict[str, Dict] = {}
    category_counts: Dict[str, int] = {}
    country_counts: Dict[str, int] = {}
    for ch in channels:
        cid = ch.get("id")
        if not cid or cid in blocked:
            continue
        if ch.get("closed") or ch.get("replaced_by"):
            continue
        if ch.get("is_nsfw") and not include_nsfw:
            continue
        ch_streams = streams_by_channel.get(cid)
        if not ch_streams:
            continue
        ch_streams.sort(key=lambda s: _quality_rank(s["quality"]), reverse=True)
        cats = [c for c in (ch.get("categories") or []) if c in category_names]
        country = ch.get("country") or ""
        records[cid] = {
            "id": cid,
            "name": ch.get("name") or cid,
            "alt_names": ch.get("alt_names") or [],
            "network": ch.get("network"),
            "country": country,
            "categories": cats,
            "website": ch.get("website"),
            "logo": (logo_by_channel.get(cid) or {}).get("url"),
            "streams": ch_streams,
            # Pre-lowered haystack so search doesn't re-lower 15k names per query.
            "_search": " ".join(
                [ch.get("name") or "", ch.get("network") or ""] + (ch.get("alt_names") or [])
            ).lower(),
        }
        for c in cats:
            category_counts[c] = category_counts.get(c, 0) + 1
        if country:
            country_counts[country] = country_counts.get(country, 0) + 1

    ordered = sorted(records.keys(), key=lambda k: records[k]["name"].casefold())
    return {
        "channels": records,
        "ordered": ordered,
        "categories": sorted(
            (
                {"id": cid, "name": category_names[cid], "count": n}
                for cid, n in category_counts.items()
            ),
            key=lambda c: c["name"],
        ),
        "countries": sorted(
            (
                {
                    "code": code,
                    "name": country_names.get(code, {}).get("name", code),
                    "flag": country_names.get(code, {}).get("flag", ""),
                    "count": n,
                }
                for code, n in country_counts.items()
            ),
            key=lambda c: (-c["count"], c["name"]),
        ),
    }


# --- The service ---------------------------------------------------------------
class IptvService:
    """In-process catalogue over the iptv-org API.

    Thread-safe like ChangelogService: the (large) network fetch runs outside
    the lock; only the catalogue swap is guarded. Unlike the changelog, a
    refresh is ~25 MB, so routes never fetch lazily inline — they kick a
    background refresh thread and answer ``ready: false`` until it lands
    (the frontend shows its tuning state and re-asks).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._catalog: Optional[Dict] = None
        self._fetched_at: float = 0.0      # time.monotonic() of last success
        self._refreshed_at: Optional[str] = None  # ISO wall time, for display
        self._last_error: Optional[str] = None
        self._refreshing = False

    # -- fetching --
    def _fetch_json(self, client: httpx.Client, name: str):
        resp = client.get(f"{IPTV_API_BASE}/{name}.json")
        resp.raise_for_status()
        return resp.json()

    def refresh(self) -> None:
        """Blocking fetch + join + swap. Fail-open: on error the previous
        catalogue keeps serving and the error is recorded + re-raised."""
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
                channels = self._fetch_json(client, "channels")
                streams = self._fetch_json(client, "streams")
                categories = self._fetch_json(client, "categories")
                countries = self._fetch_json(client, "countries")
                logos = self._fetch_json(client, "logos")
                blocklist = self._fetch_json(client, "blocklist")
            catalog = build_catalog(
                channels, streams, categories, countries, logos, blocklist,
                include_nsfw=_include_nsfw(),
            )
            with self._lock:
                self._catalog = catalog
                self._fetched_at = time.monotonic()
                self._refreshed_at = datetime.now(timezone.utc).isoformat()
                self._last_error = None
            logger.info(
                "IPTV catalogue refreshed: %d channels, %d categories, %d countries",
                len(catalog["channels"]), len(catalog["categories"]), len(catalog["countries"]),
            )
        except Exception as e:
            with self._lock:
                self._last_error = f"{type(e).__name__}: {e}"
            raise

    def ensure_refresh_started(self) -> None:
        """Kick a background refresh if the catalogue is missing/stale and no
        refresh is already in flight. Never blocks the caller."""
        with self._lock:
            fresh = (
                self._catalog is not None
                and (time.monotonic() - self._fetched_at) < _refresh_hours() * 3600
            )
            if fresh or self._refreshing:
                return
            self._refreshing = True

        def _run():
            try:
                self.refresh()
            except Exception as e:
                logger.error(f"IPTV catalogue refresh failed: {e}")
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=_run, name="iptv-refresh", daemon=True).start()

    # -- reading --
    def _snapshot(self) -> Optional[Dict]:
        with self._lock:
            return self._catalog

    @property
    def ready(self) -> bool:
        return self._snapshot() is not None

    def status(self) -> Dict:
        with self._lock:
            return {
                "ready": self._catalog is not None,
                "refreshed_at": self._refreshed_at,
                "error": self._last_error,
                "total": len(self._catalog["channels"]) if self._catalog else 0,
            }

    def browse_facets(self) -> Dict:
        cat = self._snapshot()
        if not cat:
            return {"categories": [], "countries": [], "total": 0}
        return {
            "categories": cat["categories"],
            "countries": cat["countries"],
            "total": len(cat["channels"]),
        }

    @staticmethod
    def _shape_card(rec: Dict) -> Dict:
        """One channel → the browse-card shape (no streams, no private fields)."""
        return {
            "id": rec["id"],
            "name": rec["name"],
            "country": rec["country"],
            "categories": rec["categories"],
            "logo": rec["logo"],
            "best_quality": rec["streams"][0]["quality"] if rec["streams"] else None,
            "stream_count": len(rec["streams"]),
        }

    def list_channels(
        self,
        category: Optional[str] = None,
        country: Optional[str] = None,
        q: Optional[str] = None,
        page: int = 1,
        page_size: int = 60,
    ) -> Dict:
        cat = self._snapshot()
        if not cat:
            return {"channels": [], "total": 0, "page": page, "page_size": page_size}
        needle = (q or "").strip().lower()
        country = (country or "").strip().upper()
        category = (category or "").strip().lower()

        matches = []
        for cid in cat["ordered"]:
            rec = cat["channels"][cid]
            if category and category not in rec["categories"]:
                continue
            if country and rec["country"] != country:
                continue
            if needle and needle not in rec["_search"]:
                continue
            matches.append(rec)

        page = max(1, page)
        page_size = max(1, min(200, page_size))
        start = (page - 1) * page_size
        return {
            "channels": [self._shape_card(r) for r in matches[start:start + page_size]],
            "total": len(matches),
            "page": page,
            "page_size": page_size,
        }

    def get_channel(self, channel_id: str) -> Optional[Dict]:
        """Full detail for the watch page.

        Playback is direct-first: the client plays ``direct_url`` straight off
        the broadcaster's CDN whenever ``direct_ok`` (https + no header
        requirements — measured ~55-60% of the live catalogue also serves CORS)
        and falls back to the signed ``proxy_path`` only when the browser
        can't: plain-http streams (mixed content), Referer/UA-gated feeds, or a
        CDN that serves no CORS (surfaces as a fatal hls.js network error).
        """
        cat = self._snapshot()
        if not cat:
            return None
        rec = cat["channels"].get(channel_id)
        if not rec:
            return None
        return {
            "id": rec["id"],
            "name": rec["name"],
            "network": rec["network"],
            "country": rec["country"],
            "categories": rec["categories"],
            "website": rec["website"],
            "logo": rec["logo"],
            "streams": [
                {
                    "quality": s["quality"],
                    "label": s["label"],
                    "direct_url": s["url"],
                    # Direct-eligible: an https page can only load https media,
                    # and the browser can't send a custom Referer/User-Agent.
                    # (CORS can't be known server-side — the client discovers it
                    # by trying, then falls back to the proxy.)
                    "direct_ok": s["url"].startswith("https://")
                    and not s["referrer"]
                    and not s["user_agent"],
                    "proxy_path": proxy_path(s["url"], s["referrer"], s["user_agent"]),
                }
                for s in rec["streams"]
            ],
        }


# --- Proxy fetch (used by the /iptv_proxy route) --------------------------------
async def proxy_fetch(url: str, referrer: str = "", user_agent: str = "",
                      range_header: Optional[str] = None):
    """Fetch one signed upstream URL and return
    ``(status, content_type, forward_headers, body)`` — rewritten bytes for an
    HLS playlist, an async byte-iterator for a media segment.

    Uses the SSRF-guarded client: iptv-org indexes arbitrary third-party hosts
    and playlists/redirects could otherwise steer the backend at internal
    addresses. Raises ``ValueError`` (incl. SSRFError) for the route's 403.
    """
    headers = {"User-Agent": user_agent or DEFAULT_UA}
    if referrer:
        headers["Referer"] = referrer
    if range_header:
        headers["Range"] = range_header

    client = guarded_client(
        follow_redirects=True,
        timeout=httpx.Timeout(15.0, read=30.0),
        headers=headers,
    )
    req = client.build_request("GET", url)
    resp = await client.send(req, stream=True)

    content_type = resp.headers.get("content-type", "application/octet-stream")
    final_url = str(resp.url)

    if is_playlist(content_type, final_url):
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        text = rewrite_playlist(
            raw.decode("utf-8", errors="replace"), final_url, referrer, user_agent
        )
        return resp.status_code, "application/vnd.apple.mpegurl", {}, text.encode("utf-8")

    forward = {
        h: resp.headers[h]
        for h in ("content-range", "accept-ranges", "content-length", "cache-control")
        if h in resp.headers
    }

    async def body_iter():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return resp.status_code, content_type, forward, body_iter()
