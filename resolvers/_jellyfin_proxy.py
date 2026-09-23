"""The ``/jellyfin_proxy`` transport: token injection and HLS playlist rewriting.

Every upstream request carries the access token, and every playlist is rewritten
so its sub-resources come back through this proxy with the token stripped. The
browser never sees the token, and the Jellyfin server can stay LAN-only with no
CORS setup.
"""

import re
from typing import AsyncIterator, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from ._jellyfin_client import _ensure_auth, auth_header, get_config, is_configured, reauth

PROXY_PREFIX = "/jellyfin_proxy"


def strip_api_key(url: str) -> str:
    low = url.lower()
    if ("api_key" not in low and "apikey" not in low) or "?" not in url:
        return url
    base, q = url.split("?", 1)
    pairs = [(k, v) for k, v in parse_qsl(q, keep_blank_values=True) if k.lower() not in ("api_key", "apikey")]
    return base + ("?" + urlencode(pairs) if pairs else "")


def route_through_proxy(url: str, jellyfin_url: str) -> str:
    """Point a playlist URL at the proxy and drop its token. A relative URL is
    left alone: it already resolves under the proxy path the playlist came from."""
    if url.startswith(jellyfin_url):
        url = PROXY_PREFIX + url[len(jellyfin_url):]
    elif url.startswith(("http://", "https://")):
        p = urlparse(url)
        if p.netloc != urlparse(jellyfin_url).netloc:
            return strip_api_key(url)
        # Same host under a different scheme or base path.
        url = PROXY_PREFIX + p.path + (("?" + p.query) if p.query else "")
    elif url.startswith("/"):
        url = PROXY_PREFIX + url
    return strip_api_key(url)


def rewrite_playlist(text: str, jellyfin_url: str) -> str:
    def proxied_uri(match: re.Match[str]) -> str:
        return 'URI="' + route_through_proxy(match.group(1), jellyfin_url) + '"'

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            # EXT-X-MEDIA, EXT-X-KEY and EXT-X-I-FRAME-STREAM-INF carry URI="...".
            out.append(re.sub(r'URI="([^"]+)"', proxied_uri, line))
        else:
            out.append(route_through_proxy(stripped, jellyfin_url))
    return "\n".join(out)


async def proxy_fetch(
    path: str,
    query_string: str = "",
    method: str = "GET",
    body: Optional[bytes] = None,
    range_header: Optional[str] = None,
) -> Tuple[int, str, dict, Union[bytes, AsyncIterator[bytes]]]:
    """Fetch ``{JELLYFIN_URL}/{path}?{query_string}`` with the token injected, as
    ``(status, content_type, forward_headers, body)``.

    ``body`` is the rewritten playlist as bytes, or an async iterator streaming a
    segment or file with Range passthrough. Raises ValueError when Jellyfin is
    not configured."""
    if not is_configured():
        raise ValueError("Jellyfin not configured")
    jellyfin_url, _, _ = get_config()

    async def open_upstream(token: str):
        qs = query_string or ""
        if "api_key=" not in qs.lower() and "apikey=" not in qs.lower():
            qs = (qs + "&" if qs else "") + "api_key=" + token
        headers = {"Authorization": auth_header(token)}
        if range_header:
            headers["Range"] = range_header
        # A client of its own rather than the shared one: a media stream holds its
        # connection for the whole playback and would starve the shared pool.
        client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=None))
        req = client.build_request(method, f"{jellyfin_url}/{path.lstrip('/')}?{qs}", content=body)
        return client, await client.send(req, stream=True)

    token, _uid = await _ensure_auth()
    client, resp = await open_upstream(token)
    if resp.status_code == 401:
        await resp.aclose()
        await client.aclose()
        token, _uid = await reauth(token)
        client, resp = await open_upstream(token)

    content_type = resp.headers.get("content-type", "application/octet-stream")

    if "mpegurl" in content_type.lower():
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        text = rewrite_playlist(raw.decode("utf-8", errors="replace"), jellyfin_url)
        return resp.status_code, content_type, {}, text.encode("utf-8")

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
