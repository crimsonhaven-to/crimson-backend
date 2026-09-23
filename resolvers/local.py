"""Resolves a ``crimson-local:{token}`` marker from an admin-registered directory.

Browser-native containers direct-play through ``/local_proxy``; anything else
goes through the on-the-fly HLS transcode, but only for a source with encoding
enabled. The ``local_engine.fs`` choke points own that policy, so this only maps
their verdict to a path.
"""

from local_engine.fs import (
    EMBED_MARKER,
    HLS_PREFIX,
    PROXY_PREFIX,
    is_configured,
    safe_resolve,
    safe_resolve_transcode,
)

from ._marker import marker_token
from .base_resolver import BaseResolver


class LocalResolver(BaseResolver):
    domain_keyword: str = EMBED_MARKER
    source_name: str = "Local"

    async def resolve(self, embed_url: str) -> str | None:
        if not is_configured():
            return None
        token = marker_token(embed_url)
        if not token:
            return None
        # Direct play first: it is cheaper than a transcode.
        if safe_resolve(token):
            return f"{PROXY_PREFIX}/{token}"
        if safe_resolve_transcode(token):
            return f"{HLS_PREFIX}/{token}/master.m3u8"
        return None
