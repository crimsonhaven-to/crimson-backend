"""
Template scraper — a documented, no-op reference implementation.

This is the ONLY "source-shaped" scraper left in the public backend. The backend
no longer scrapes third-party streaming sites: that logic lives in the private
``crimson-sources`` package and runs in the client / browser extension /
crimson-proxy (see ``New_System.md``). What remains here are the two operator-
owned sources that are *not* third-party scraping — ``LocalScraper`` (your own
NAS / bind-mounts) and ``CacheScraper`` (episodes this server already remuxed) —
plus the ``JellyfinScraper`` (your own self-hosted media server).

Keep this file as living documentation of the scraper contract: it shows exactly
what a class in ``ALL_SCRAPERS`` must implement and how it plugs into the unified
``search → embeds → resolve`` watch pipeline (``run_single_scraper`` in api.py).
It is wired into ``ALL_SCRAPERS`` but is inert by default — ``search_anime``
returns ``None``, so the pipeline short-circuits and it never emits a stream.

To turn it into a real, *legal* source (e.g. another personal media server you
control), implement the two methods below and emit a marker embed that a matching
resolver in ``resolvers/`` turns into a playable URL. Do NOT use it to scrape
sites you don't own — that's exactly what this backend was split apart to avoid.
"""

from __future__ import annotations

from typing import List, Optional

from .base_scraper import BaseAnimeScraper


class TemplateScraper(BaseAnimeScraper):
    """Reference no-op source. Copy this to build an operator-owned source.

    A scraper's job is two steps:

      1. ``search_anime``       — map the request's media context (titles + TMDB
                                  id) to an opaque identifier for the target
                                  (a slug, a path, an item id, …), or ``None``
                                  to opt out of this request.
      2. ``get_episode_embeds`` — given that identifier + season/episode, return
                                  a list of *embeds*. Each embed is either a bare
                                  URL string or a ``{"url": …, "language": …}``
                                  dict. A matching resolver (keyed on a substring
                                  of the URL via ``domain_keyword``) later turns
                                  each embed into a concrete stream.

    The convention for operator-owned sources is to emit a ``crimson-<name>:<token>``
    marker (an internal routing token, NOT a third-party URL) and pair it with a
    resolver that decodes the token — see ``scrapers/local_scraper.py`` +
    ``resolvers/local.py`` for a complete worked example.
    """

    # Set True only if this source can serve a standalone movie (a TMDB *movie*
    # id with no season/episode). See ``CacheScraper`` for a movie-aware source.
    SUPPORTS_MOVIES = False

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        """Resolve ``media_ctx`` to this source's identifier, or ``None`` to skip.

        ``media_ctx`` carries (all optional except the ids the pipeline always
        sets): ``tmdb_id``, ``tmdb_season``, ``media_type`` ("tv"/"movie"),
        ``title``, ``title_english``, ``title_romaji``, ``title_native`` and
        ``synonyms``. Returning a falsy value short-circuits the pipeline for this
        request — which is what this template does, so it stays inert.
        """
        return None

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> List:
        """Return embeds for the requested episode (empty here — no-op).

        A real implementation returns e.g. ``[f"crimson-template:{token}"]`` and
        ships a ``resolvers/template.py`` whose ``domain_keyword`` matches
        ``"crimson-template:"`` and whose ``resolve`` turns the token into a
        playable (usually same-origin proxied) stream URL.
        """
        return []
