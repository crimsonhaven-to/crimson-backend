"""
Static descriptors behind the dashboard's Source Health view. The probing itself
lives in api.py, which owns the pipeline; declaring the canary, labels and
env-gates here keeps that probe a thin loop over this table.

Two categories:

* ``scrape`` is an external site. Health asks whether it still surfaces embeds
  for the canary title: green ok, yellow empty, red error.
* ``library`` is operator-provided (the cache, NAS dirs, a personal Jellyfin).
  It holds only what the operator added, so a fixed canary proves nothing;
  health asks whether it is configured and non-empty.

Keyed by scraper class ``__name__``.
"""

from __future__ import annotations

from core.config import get_settings


def canary() -> dict:
    """The title every scrape source is probed with. The default carries both an
    AniList and a TMDB mapping, so every kind of scraper can attempt it."""
    s = get_settings()
    return {
        "title": s.health_canary_title,
        "tmdb_id": s.health_canary_tmdb,
        "season": s.health_canary_season,
        "episode": s.health_canary_episode,
        "anilist_id": s.health_canary_anilist,
    }

# Only ``library`` sources remain, since the public backend no longer scrapes
# third-party sites (that moved to the private crimson-sources package, see
# New_System.md). The ``scrape`` category and canary stay for the contract and any
# future operator-owned source wanting an end-to-end probe.
SOURCE_META = {
    # --- operator-provided library sources ---------------------------------
    "CacheScraper":    {"label": "Server Cache", "category": "library", "note": "Remuxed episodes on your NAS"},
    "LocalScraper":    {"label": "Local Media",  "category": "library", "note": "Registered NAS / bind-mount dirs"},
    "JellyfinScraper": {"label": "Jellyfin",     "category": "library", "note": "Your personal Jellyfin server"},
    # --- documentation-only template ---------------------------------------
    "TemplateScraper": {"label": "Template",     "category": "library", "note": "Inert reference source (no-op)"},
}


def meta_for(class_name: str) -> dict:
    """Descriptor for a scraper class name, defaulted for an unlisted one."""
    return SOURCE_META.get(class_name, {"label": class_name, "category": "scrape", "note": None})
