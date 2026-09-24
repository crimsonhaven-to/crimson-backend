"""The seam an operator build fills with a server-side manga source.

The public backend never talks to a manga host: chapters and pages resolve in the
viewer's browser (crimson-sources). An operator build may drop a module into this
package declaring a module-level ``MANGA_PROVIDER`` that satisfies
:class:`MangaProvider`, so extension-less devices can read server-side. A base
build has none and ``get_provider()`` returns ``None``.
"""

from __future__ import annotations

from functools import cache
from typing import List, Optional, Protocol, Tuple


class MangaProvider(Protocol):
    """The three resolve stages the client engine performs, plus the signed image
    relay an operator build serves same-origin."""

    def configured(self) -> bool: ...

    async def resolve_manga_id(self, titles: List[str]) -> Optional[str]: ...

    async def get_chapters(self, manga_id: str, language: Optional[str] = None) -> List[dict]: ...

    async def get_chapter_pages(
        self, chapter_id: str, base_url: str = "", data_saver: bool = False
    ) -> List[str]: ...

    async def proxy_fetch(
        self, url: Optional[str], sig: Optional[str], range_header: Optional[str] = None
    ) -> Tuple[int, str, dict, bytes]: ...


@cache
def get_provider() -> Optional[MangaProvider]:
    """The injected provider, or ``None``. The overlay is fixed at process start."""
    import manga_engine
    from core.private_sources import discover_provider

    try:
        return discover_provider(manga_engine, "MANGA_PROVIDER")
    except Exception:
        return None
