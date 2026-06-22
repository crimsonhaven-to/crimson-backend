"""
Cache resolver — turns a ``crimson-cache:{token}`` marker (emitted by the
CacheScraper for an already-downloaded episode) into a direct-play
``/cache_proxy/{token}`` path.

The token is an opaque base64url of the cached file's absolute path. This
resolver decodes it, confirms the file still lives inside a *currently enabled*
cache target, and labels the resulting stream with that target's admin-given
**name** — so the source the viewer sees is the NAS cache's name (e.g. "Crimson
Vault"), not a fixed string. Renaming/disabling a target in the dashboard takes
effect immediately: rename changes the label, disable makes the source vanish.

Like the Local source, the bytes come straight off disk, so api.py serves
``/cache_proxy`` with HTTP Range support (FileResponse) and there's no playlist
rewriting. See [[jellyfin-source]] / local.py for the direct-play pattern.
"""

from typing import Optional, Union

from cache_engine.fs import EMBED_MARKER, PROXY_PREFIX, decode_token, target_for_path

from .base_resolver import BaseResolver


class CacheResolver(BaseResolver):
    """Resolves ``crimson-cache:{token}`` to ``{/cache_proxy/{token}, <target name>}``."""

    domain_keyword: str = EMBED_MARKER
    source_name: str = "Cache"  # fallback label; normally overridden per target

    async def resolve(self, embed_url: str) -> Optional[Union[str, dict]]:
        token = embed_url.split(":", 1)[1] if ":" in embed_url else embed_url
        token = token.strip().strip("/")
        if not token:
            return None
        path = decode_token(token)
        if not path:
            return None
        target = target_for_path(path)
        if not target:
            return None  # target disabled/removed → source disappears
        # Returning a dict lets resolve_streams stamp a per-stream source label
        # (the target name) instead of the static source_name.
        return {"url": f"{PROXY_PREFIX}/{token}", "source": target["name"]}
