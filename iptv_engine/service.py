"""Live TV: a read-only catalogue of free-to-air channels from the iptv-org index
(https://iptv-org.github.io/api/).

Nothing is hosted here. Each refresh fetches the index, drops blocklisted,
closed and (by default) NSFW channels plus any without a stream, and keeps the
joined result in process. A refresh is about 25 MB, so it runs in a background
thread, per replica.

Playback prefers the broadcaster's CDN and falls back to the signed
``/iptv_proxy``: about a fifth of the streams are plain http (mixed content on an
https page), many send no CORS, and some need a Referer or User-Agent a browser
cannot set.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import quote, urljoin

import httpx

from functools import cache

from core import signing
from core.clock import utc_now_iso
from core.config import get_settings
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


# Every URL /iptv_proxy may fetch is signed, so it is not an open relay. The
# signature covers the Referer and User-Agent too, or a valid signature could be
# replayed with attacker-chosen headers.
@cache
def _proxy_secret() -> bytes:
    return signing.resolve_secret("IPTV_PROXY_SECRET")


def _stream_payload(url: str, referrer: str, user_agent: str) -> str:
    return "\n".join((url, referrer or "", user_agent or ""))


def sign_stream(url: str, referrer: str = "", user_agent: str = "") -> str:
    return signing.sign(_proxy_secret(), _stream_payload(url, referrer, user_agent))


def verify_stream_sig(url: str, sig: str, referrer: str = "", user_agent: str = "") -> bool:
    return signing.verify(_proxy_secret(), _stream_payload(url, referrer, user_agent), sig)


def proxy_path(url: str, referrer: str = "", user_agent: str = "") -> str:
    parts = [
        f"{PROXY_PREFIX}?u={quote(url, safe='')}",
        f"s={sign_stream(url, referrer, user_agent)}",
    ]
    if referrer:
        parts.append(f"r={quote(referrer, safe='')}")
    if user_agent:
        parts.append(f"a={quote(user_agent, safe='')}")
    return "&".join(parts)


def rewrite_playlist(text: str, base_url: str, referrer: str = "", user_agent: str = "") -> str:
    """Route every URI in an m3u8, including ``URI="..."`` tag attributes, back
    through /iptv_proxy. ``base_url`` is the upstream URL after redirects, which
    relative URIs resolve against."""

    def _route(uri: str) -> str:
        uri = uri.strip()
        if not uri or uri.startswith("data:"):
            return uri
        return proxy_path(urljoin(base_url, uri), referrer, user_agent)

    def _route_attribute(match: re.Match[str]) -> str:
        return 'URI="' + _route(match.group(1)) + '"'

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(
                re.sub(r'URI="([^"]+)"', _route_attribute, line)
            )
        else:
            out.append(_route(stripped))
    return "\n".join(out)


def is_playlist(content_type: str, url: str) -> bool:
    if "mpegurl" in (content_type or "").lower():
        return True
    return url.split("?", 1)[0].lower().endswith((".m3u8", ".m3u"))


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
    """Join the raw iptv-org payloads. Only live, permitted channels with at least
    one stream make it in."""
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
            # Lowered once here so a search does not re-lower 15k names per query.
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


class IptvService:
    """The fetch runs outside the lock; only the catalogue swap is guarded. Routes
    never fetch inline: they start a background refresh and answer
    ``ready: false`` until it lands."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._catalog: Optional[Dict] = None
        self._fetched_at: float = 0.0
        self._refreshed_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._refreshing = False

    def _fetch_json(self, client: httpx.Client, name: str):
        resp = client.get(f"{IPTV_API_BASE}/{name}.json")
        resp.raise_for_status()
        return resp.json()

    def refresh(self) -> None:
        """Blocking. On error the previous catalogue keeps serving and the error is
        recorded, then re-raised."""
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
                include_nsfw=get_settings().iptv_include_nsfw,
            )
            with self._lock:
                self._catalog = catalog
                self._fetched_at = time.monotonic()
                self._refreshed_at = utc_now_iso()
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
        with self._lock:
            fresh = (
                self._catalog is not None
                and (time.monotonic() - self._fetched_at) < get_settings().iptv_refresh_hours * 3600
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
        """The client plays ``direct_url`` when ``direct_ok`` and falls back to
        ``proxy_path`` when that fails. CORS cannot be known server-side (about
        55 to 60% of the catalogue sends it), so the client finds out by trying."""
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
                    # An https page loads only https media, and a browser cannot
                    # set a custom Referer or User-Agent.
                    "direct_ok": s["url"].startswith("https://")
                    and not s["referrer"]
                    and not s["user_agent"],
                    "proxy_path": proxy_path(s["url"], s["referrer"], s["user_agent"]),
                }
                for s in rec["streams"]
            ],
        }


async def proxy_fetch(url: str, referrer: str = "", user_agent: str = "",
                      range_header: Optional[str] = None):
    """``(status, content_type, forward_headers, body)``: rewritten bytes for a
    playlist, an async byte iterator for a segment.

    The index lists arbitrary hosts, and a playlist or redirect could otherwise
    steer the backend at internal addresses, hence the SSRF-guarded client. Raises
    ``ValueError`` (SSRFError included) for the route's 403."""
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
    try:
        resp = await client.send(client.build_request("GET", url), stream=True)
    except BaseException:
        await client.aclose()
        raise

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


service = IptvService()
