"""
ShowBox / Febbox resolver (TMDB-title-keyed direct-file source).

Unlike the embed-host sources (VOE, Vidmoly) this is a cloud **file-host**:
ShowBox indexes a title and hands back a Febbox *file share* (a folder of the
actual video files), and Febbox's player endpoint returns **direct mp4 links**
per quality. So there's no third-party player UI, no ad iframe and no rotating
obfuscation to chase — just direct files, exactly the durable shape the
Jellyfin / PlayIMDb / cinema.bz sources already use.

Division of labour (see ``scrapers/showbox_scraper.py`` for the first half):

    scraper  -> showbox.media search/slug  -> /{tv}/detail/{id}
             -> showbox.media/index/share_link -> febbox share_key
             -> febbox file_share_list (season folder -> episode file)
             -> crimson-febbox:{share_key}:{fid}        (this resolver's marker)

    resolver -> POST febbox.com/file/player  (fid + share_key, Cookie: ui=<token>)
             -> parse ``var sources = [...]`` -> pick best-quality direct mp4
             -> /febbox_proxy?u=<mp4>&s=<sig>            (type mp4 / hls)
    frontend -> CrimsonPlayer plays it like any other source (no iframe).

The make-or-break: everything up to ``/file/player`` is **open** (no auth), but
that final call returns ``{"code":-1,"msg":"please login"}`` without a logged-in
Febbox account cookie. So this whole source is **gated on FEBBOX_UI_TOKEN** — the
``ui`` cookie value from a febbox.com session (grab it from your browser's cookies
on febbox.com after logging in). With it unset the source disables itself and
silently never surfaces, exactly like the Jellyfin source without JELLYFIN_URL.

Why a proxy (not direct play): the ``var sources`` URLs live on a rotating Febbox
OSS CDN; proxying keeps playback same-origin (no CORS surprises), survives host
rotation and gives us Range passthrough for seeking. As with the cinema.bz /
PlayIMDb proxies the CDN host rotates, so every proxied URL is **HMAC-signed** and
the proxy refuses anything unsigned (closes the open-relay / SSRF hole). See
[[playimdb-source]] / [[cinemabz-source]] for the signed-HLS-proxy pattern this
mirrors and [[jellyfin-source]] for the env-gated extracted-stream shape.
"""

import hashlib
import hmac
import json
import logging
import os
import re
from typing import AsyncIterator, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlparse

import httpx
from curl_cffi.requests import AsyncSession

from .base_resolver import BaseResolver
from ._proxy_secret import resolve_secret as _resolve_proxy_secret
from ._ssrf_guard import guarded_client

logger = logging.getLogger(__name__)

# --- Routing ---------------------------------------------------------------
PROXY_PREFIX = "/febbox_proxy"
# The scraper emits "crimson-febbox:{share_key}:{fid}"; this resolver matches on
# the prefix. share_key is mixed-case alnum + hyphens, fid is digits.
EMBED_MARKER = "crimson-febbox"

FEBBOX_BASE = "https://www.febbox.com"
FEBBOX_PLAYER_URL = f"{FEBBOX_BASE}/file/player"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Quality preference, best first. Febbox labels are like "ORG"/"4K"/"1080P"/…;
# we normalise to these tokens and pick the highest available with a real file.
_QUALITY_ORDER = ("org", "4k", "2160", "1440", "1080", "720", "480", "360", "240")


def _ui_token() -> Optional[str]:
    """The febbox ``ui`` session cookie value (FEBBOX_UI_TOKEN), or None."""
    tok = (os.getenv("FEBBOX_UI_TOKEN") or "").strip()
    # Tolerate the user pasting the whole "ui=<token>" cookie pair.
    if tok.startswith("ui="):
        tok = tok[3:]
    return tok or None


def _region() -> str:
    """Febbox OSS region used in the cookie (some shares are region-gated)."""
    return (os.getenv("FEBBOX_REGION") or "USA7").strip()


def is_configured() -> bool:
    """True only when a FEBBOX_UI_TOKEN is set. Without it the player call 401s
    (``please login``), so the whole ShowBox/Febbox source disables itself —
    the scraper short-circuits and no dead tile ever surfaces."""
    return _ui_token() is not None


# --- Proxy URL signing -----------------------------------------------------
# The proxy fetches rotating Febbox OSS hosts, so an unsigned proxy would be an
# open relay / SSRF vector. Every proxied URL carries an HMAC the proxy verifies
# before fetching. The secret must be stable + shared across replicas (a link
# minted by one replica is verified by whichever replica the player's next
# request lands on); see resolvers._proxy_secret.
_SECRET = _resolve_proxy_secret("FEBBOX_PROXY_SECRET")


def _sign(url: str) -> str:
    return hmac.new(_SECRET, url.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _verify(url: str, sig: str) -> bool:
    return bool(url) and hmac.compare_digest(_sign(url), sig or "")


def _proxy_path_for(upstream_url: str) -> str:
    """Same-origin, signed proxy path for an absolute upstream URL."""
    return f"{PROXY_PREFIX}?u={quote(upstream_url, safe='')}&s={_sign(upstream_url)}"


# --- HLS playlist rewriting ------------------------------------------------
# Febbox normally serves progressive mp4, but some qualities come back as HLS;
# handle both so an m3u8 still flows wholly through our origin.
def _is_playlist_url(url: str) -> bool:
    return ".m3u8" in urlparse(url).path.lower()


def rewrite_playlist(text: str, base_url: str) -> str:
    """Rewrite an HLS m3u8 so every sub-resource is absolutized against
    ``base_url`` and routed back through this signed proxy."""
    def _route(raw: str) -> str:
        return _proxy_path_for(urljoin(base_url, raw.strip()))

    out: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            if 'URI="' in stripped:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="' + _route(m.group(1)) + '"',
                    line,
                )
            out.append(line)
        else:
            out.append(_route(stripped))
    return "\n".join(out)


# --- The proxy fetch (used by the api.py route) ----------------------------
async def proxy_fetch(
    url: Optional[str],
    sig: Optional[str],
    range_header: Optional[str] = None,
) -> Tuple[int, str, dict, Union[bytes, AsyncIterator[bytes]]]:
    """Fetch a signed upstream Febbox file URL and return
    ``(status_code, content_type, forward_headers, body)``.

    ``body`` is rewritten ``bytes`` for HLS playlists and an async byte-iterator
    for media (mp4 / segments), streamed through with Range passthrough so
    seeking works. Raises ``ValueError`` for a missing/invalid signature (keeps
    the proxy from being an open relay)."""
    if not url or not url.startswith("https://"):
        raise ValueError("Missing or non-https upstream URL")
    if not _verify(url, sig):
        raise ValueError("Bad or missing signature")

    req_headers = {"User-Agent": UA, "Accept": "*/*"}
    if range_header:
        req_headers["Range"] = range_header

    # No read timeout: media files are large.
    client = guarded_client(
        headers=req_headers,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=None),
    )
    req = client.build_request("GET", url)
    resp = await client.send(req, stream=True)
    content_type = resp.headers.get("content-type", "application/octet-stream")

    if _is_playlist_url(url):
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        text = rewrite_playlist(raw.decode("utf-8", errors="replace"), url)
        return resp.status_code, "application/vnd.apple.mpegurl", {}, text.encode("utf-8")

    forward = {
        h: resp.headers[h]
        for h in ("content-range", "accept-ranges", "content-length", "cache-control")
        if h in resp.headers
    }

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return resp.status_code, content_type, forward, body_iter()


# --- Febbox player lookup --------------------------------------------------
def _quality_rank(label: str) -> int:
    """Rank a febbox source label; lower = better (earlier in _QUALITY_ORDER)."""
    low = (label or "").lower()
    for i, tok in enumerate(_QUALITY_ORDER):
        if tok in low:
            return i
    return len(_QUALITY_ORDER)  # unknown labels sort last


async def fetch_best_stream(share_key: str, fid: str) -> Optional[str]:
    """POST the Febbox player endpoint for one file and return the best-quality
    direct file URL, or None.

    Requires FEBBOX_UI_TOKEN — without the ``ui`` cookie the endpoint answers
    ``{"code":-1,"msg":"please login"}`` and we get nothing."""
    token = _ui_token()
    if not token:
        return None

    headers = {
        "Cookie": f"ui={token}; oss_group={_region()}",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": FEBBOX_BASE,
        "Referer": f"{FEBBOX_BASE}/share/{share_key}",
        "User-Agent": UA,
        "Accept": "*/*",
    }

    # curl_cffi (TLS impersonation) for the API call — febbox fingerprints plain
    # clients; the streaming proxy above stays on guarded httpx for SSRF safety.
    session = AsyncSession(impersonate="chrome", timeout=20.0, allow_redirects=True)
    try:
        resp = await session.post(
            FEBBOX_PLAYER_URL, data={"fid": str(fid), "share_key": share_key}, headers=headers
        )
        body = resp.text
    finally:
        await session.close()

    match = re.search(r"var\s+sources\s*=\s*(\[.*?\])\s*;", body, re.S)
    if not match:
        # Surface the login wall / region error explicitly — it's the usual cause.
        try:
            msg = json.loads(body).get("msg")
            logger.warning(f"[febbox] no sources for fid {fid}: {msg!r}")
        except (json.JSONDecodeError, AttributeError):
            logger.warning(f"[febbox] no sources for fid {fid} (unparseable response)")
        return None

    try:
        sources = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning(f"[febbox] could not parse sources array for fid {fid}")
        return None

    candidates = [
        s for s in sources
        if isinstance(s, dict) and isinstance(s.get("file"), str) and s["file"].startswith("http")
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda s: _quality_rank(s.get("label", "")))
    best = candidates[0]
    logger.info(f"[febbox] fid {fid}: picked {best.get('label')!r} of "
                f"{[c.get('label') for c in candidates]}")
    return best["file"]


class FebboxResolver(BaseResolver):
    """Resolves a ``crimson-febbox:{share_key}:{fid}`` marker to a backend-proxied
    direct file (ShowBox/Febbox source).

    Disabled (returns None) unless FEBBOX_UI_TOKEN is set. Returns a signed
    ``/febbox_proxy`` path (tagged hls/mp4 by api.py — not a /player iframe), or
    None when the file can't be unlocked (login wall, region gate, expired share).
    """

    domain_keyword: str = EMBED_MARKER
    source_name: str = "ShowBox"

    async def resolve(self, embed_url: str) -> Optional[str]:
        if not is_configured():
            return None

        # Marker: crimson-febbox:{share_key}:{fid}  (share_key keeps its case;
        # resolve_streams only lowercases for the keyword *match*, not here).
        parts = embed_url.split(":")
        if len(parts) < 3 or parts[0] != EMBED_MARKER:
            logger.warning(f"[febbox] unrecognised marker: {embed_url}")
            return None
        share_key, fid = parts[1], parts[2]

        try:
            best = await fetch_best_stream(share_key, fid)
        except httpx.RequestError as e:
            logger.warning(f"[febbox] request failed: {type(e).__name__} - {e}")
            return None
        except Exception as e:  # curl_cffi raises its own error types
            logger.warning(f"[febbox] player fetch failed: {type(e).__name__} - {e}")
            return None

        if not best:
            return None

        proxy_path = _proxy_path_for(best)
        logger.info(f"[febbox] resolved fid {fid} -> proxied stream ({best[:60]}…)")
        return proxy_path
