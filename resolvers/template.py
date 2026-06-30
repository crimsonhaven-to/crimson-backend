"""
Template resolver — the documented counterpart to ``scrapers/template_scraper.py``.

A resolver's job is to turn an *embed* (emitted by a scraper) into something the
frontend player can actually load. It is matched to an embed by a simple
substring test: ``resolve_streams`` (api.py) picks the first resolver whose
``domain_keyword`` appears in the embed URL, then calls ``resolve``.

``resolve`` may return:
  * ``None``                       — nothing playable (the source is dropped);
  * a ``str``                      — a stream URL. A leading ``/`` is treated as
                                     a same-origin backend path and absolutized
                                     against the request base URL; ``.m3u8`` →
                                     HLS, otherwise progressive MP4; a
                                     ``/..._proxy/h/`` or ``/player`` path is
                                     wrapped in an ``<iframe>``;
  * a ``dict``                     — ``{"url", optional "source", "subtitles"}``
                                     to override the display label per-stream or
                                     attach external subtitle tracks;
  * a ``list[dict]``               — fan one embed out into several stream tiles
                                     (e.g. multiple qualities / languages).

This template is inert: nothing emits a ``crimson-template:`` marker (the
template scraper is a no-op), so ``resolve`` is never reached in practice. It
exists as living documentation of the contract — see ``resolvers/local.py`` and
``resolvers/cache.py`` for complete, working operator-owned resolvers.
"""

from __future__ import annotations

from typing import Optional, Union

from .base_resolver import BaseResolver


class TemplateResolver(BaseResolver):
    """Reference no-op resolver. Copy this to pair with an operator-owned source."""

    # Substring matched against an embed URL to claim it. Operator-owned sources
    # use a ``crimson-<name>:`` marker prefix so the embed is an internal routing
    # token, never a third-party URL.
    domain_keyword: str = "crimson-template:"

    # Default display label for tiles this resolver produces (a dict return value
    # may override it per-stream).
    source_name: str = "Template"

    async def resolve(self, embed_url: str) -> Optional[Union[str, dict, list]]:
        """Turn an embed marker into a playable stream — no-op in the template.

        A real implementation decodes ``embed_url`` (typically
        ``crimson-template:<token>``), verifies the target is still configured /
        in-bounds, and returns a same-origin proxy path (e.g.
        ``/template_proxy/<token>``) or an absolute stream URL. Returning ``None``
        cleanly drops the source so it never surfaces as a dead tile.
        """
        return None
