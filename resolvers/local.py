"""
Local source resolver — streaming from an admin-registered directory / NAS mount
(a Jellyfin-lite).

The "Local" scraper locates a media file on disk and emits ``crimson-local:{token}``
(an opaque base64url of the absolute path). This resolver turns that into one of two
same-origin paths, depending on the file and the source's per-root ``encoding`` flag:

  * ``/local_proxy/{token}`` — direct play (HTTP Range, seekable) for browser-native
    containers (mp4/m4v/mov/webm). The route just streams the bytes.
  * ``/local_hls/{token}/master.m3u8`` — on-the-fly HLS transcode (also seekable) for
    everything else (mkv/avi/ts/…), but ONLY when the source has encoding enabled.

Which one applies is decided by the fs choke points (``safe_resolve`` for direct,
``safe_resolve_transcode`` for HLS), so the resolver never decides playability on its
own — it just maps the validated outcome to a URL. Unlike the Jellyfin source (which
proxies a remote, auth-gated server), the bytes here come straight off a local disk.
See [[jellyfin-source]] for the proxied sibling.
"""

from typing import Optional

from local_engine.fs import (
    EMBED_MARKER,
    HLS_PREFIX,
    PROXY_PREFIX,
    is_configured,
    safe_resolve,
    safe_resolve_transcode,
)

from .base_resolver import BaseResolver


class LocalResolver(BaseResolver):
    """Resolves a ``crimson-local:{token}`` marker to a ``/local_proxy/...`` (direct
    play) or ``/local_hls/.../master.m3u8`` (transcode) path.

    Returns a *relative* path; api.py prefixes it with the request base URL so the
    frontend gets an absolute URL it can drop into the player (resolve_streams types
    an ``.m3u8`` path as hls and the proxy path as mp4 automatically).
    """

    domain_keyword: str = EMBED_MARKER
    source_name: str = "Local"

    async def resolve(self, embed_url: str) -> Optional[str]:
        if not is_configured():
            return None
        token = embed_url.split(":", 1)[1] if ":" in embed_url else embed_url
        token = token.strip().strip("/")
        if not token:
            return None
        # Direct play wins when the file is browser-native (cheaper, no transcode).
        if safe_resolve(token):
            return f"{PROXY_PREFIX}/{token}"
        # Otherwise transcode — but only if it's a transcodable file in an enabled,
        # encoding-on root (safe_resolve_transcode enforces exactly that).
        if safe_resolve_transcode(token):
            return f"{HLS_PREFIX}/{token}/master.m3u8"
        return None
