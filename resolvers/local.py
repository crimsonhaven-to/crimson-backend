"""
Local source resolver — direct-play-only streaming from an admin-registered
directory / NAS mount (a Jellyfin-lite).

The "Local" scraper locates a browser-playable file on disk and emits
``crimson-local:{token}`` (an opaque base64url of the absolute path). This
resolver turns that into a same-origin ``/local_proxy/{token}`` path, which
api.py serves with HTTP Range support (so seeking works). MVP scope is direct
play only — the file must be something a ``<video>`` tag can play as-is
(mp4/m4v/mov/webm); there is no transcoding.

Unlike the Jellyfin source (which proxies a remote, auth-gated server), the
bytes here come straight off a local disk, so there's no token injection /
playlist rewriting — the route just streams the file. See [[jellyfin-source]]
for the proxied sibling.
"""

from typing import Optional

from local_engine.fs import EMBED_MARKER, PROXY_PREFIX, is_configured

from .base_resolver import BaseResolver


class LocalResolver(BaseResolver):
    """Resolves a ``crimson-local:{token}`` marker to a ``/local_proxy/...`` path.

    Returns a *relative* path; api.py prefixes it with the request base URL so the
    frontend gets an absolute mp4 URL it can drop into the player.
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
        return f"{PROXY_PREFIX}/{token}"
