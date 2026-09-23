"""Resolves a ``crimson-cache:{token}`` marker to a direct-play ``/cache_proxy`` path.

The token is a base64url of the cached file's absolute path. The stream is
labelled with the cache target's admin-given name, and the target must still be
enabled, so renaming a target relabels the source at once and disabling it makes
the source vanish.
"""

from cache_engine.fs import EMBED_MARKER, PROXY_PREFIX, decode_token, target_for_path

from ._marker import marker_token
from .base_resolver import BaseResolver


class CacheResolver(BaseResolver):
    domain_keyword: str = EMBED_MARKER
    source_name: str = "Cache"

    async def resolve(self, embed_url: str) -> dict | None:
        token = marker_token(embed_url)
        path = decode_token(token) if token else None
        target = target_for_path(path) if path else None
        if not target:
            return None
        return {"url": f"{PROXY_PREFIX}/{token}", "source": target["name"]}
