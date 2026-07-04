"""Manga provider seam — the public backend holds NO manga source, only a hole.

The public backend deliberately never talks to any manga host itself (same posture
as the removed video scrapers): the reading pages are resolved in the viewer's own
browser by ``crimson-sources`` (E2/E3), and the backend stays a pure metadata +
orchestration layer (AniList discovery, the reader UI contract). So this module
defines only the *shape* of a manga source and a discovery hook — the concrete
implementation is absent from a base build.

An operator who wants extension-less devices to still read server-side can inject a
private provider via the build-time overlay (see ``core.private_sources`` and the
self-hosting docs): a module dropped into this package that declares a module-level
``MANGA_PROVIDER`` instance satisfying :class:`MangaProvider`. ``get_provider()``
finds it; a base build finds nothing and returns ``None``, so the chapter/page
routes simply report "unmapped" and the client fills them in.

The only thing that stays public is *preference config* (which languages / content
ratings to surface) — it names no host, and the client reads it back from the
overview response so a client-resolved chapter list matches what a provider build
would have produced.
"""

from __future__ import annotations

import os
from typing import List, Optional, Protocol, Tuple, runtime_checkable

# --- public preference config (names no host) -------------------------------


def _clean_list_env(name: str, default: str) -> List[str]:
    raw = os.getenv(name) or default
    return [p.strip() for p in raw.split(",") if p.strip()]


def manga_enabled() -> bool:
    """Master switch for the whole reading surface (AniList discovery + the reader
    UI), independent of whether a server-side provider is present. Default on."""
    return (os.getenv("MANGA_ENABLED", "true").strip().lower()
            not in ("0", "false", "no", "off"))


def preferred_languages() -> List[str]:
    """Chapter languages to surface, in priority order (default ``en``)."""
    return _clean_list_env("MANGA_LANGUAGES", "en")


def default_language() -> str:
    langs = preferred_languages()
    return langs[0] if langs else "en"


def content_ratings() -> List[str]:
    """Content ratings to include (default ``safe,suggestive,erotica``;
    ``pornographic`` is opt-in). Threaded to the client so its search matches."""
    return _clean_list_env("MANGA_CONTENT_RATING", "safe,suggestive,erotica")


# --- provider protocol (the injected private implementation satisfies this) --


@runtime_checkable
class MangaProvider(Protocol):
    """A server-side manga source. Implemented only by the private overlay; the
    public backend never ships one. Mirrors the three resolve stages the client
    engine performs (find id → list chapters → fetch page images) plus the signed
    image relay an operator build serves same-origin."""

    def configured(self) -> bool: ...

    async def resolve_manga_id(self, titles: List[str]) -> Optional[str]: ...

    async def get_chapters(self, manga_id: str, language: Optional[str] = None) -> List[dict]: ...

    async def get_chapter_pages(
        self, chapter_id: str, base_url: str = "", data_saver: bool = False
    ) -> List[str]: ...

    # Image relay (only used when this provider is present, i.e. an operator build).
    async def proxy_fetch(
        self, url: Optional[str], sig: Optional[str], range_header: Optional[str] = None
    ) -> Tuple[int, str, dict, bytes]: ...


# --- discovery --------------------------------------------------------------

_provider_cache: List[Optional[MangaProvider]] = []


def get_provider() -> Optional[MangaProvider]:
    """The injected private manga provider, or ``None`` in a base build.

    Discovery is memoized (the overlay set is fixed at process start). Scans this
    package for a module exposing a module-level ``MANGA_PROVIDER``; a base build
    has none. Off entirely via ``PRIVATE_SOURCES_ENABLED=0``."""
    if _provider_cache:
        return _provider_cache[0]

    provider: Optional[MangaProvider] = None
    try:
        import manga_engine as _pkg
        from core.private_sources import discover_manga_provider

        provider = discover_manga_provider(_pkg)
    except Exception:
        provider = None

    _provider_cache.append(provider)
    return provider
