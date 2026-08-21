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

import os

# Chosen to exist on as many sources as possible and to carry both an AniList and
# a TMDB mapping, so every kind of scraper can attempt it.
CANARY = {
    "title": os.getenv("HEALTH_CANARY_TITLE", "Attack on Titan"),
    "tmdb_id": int(os.getenv("HEALTH_CANARY_TMDB", "1429")),       # AoT (TMDB tv)
    "season": int(os.getenv("HEALTH_CANARY_SEASON", "1")),
    "episode": int(os.getenv("HEALTH_CANARY_EPISODE", "1")),
    "anilist_id": int(os.getenv("HEALTH_CANARY_ANILIST", "16498")),  # AoT (AniList)
}

# ``env_gate`` names an env var that must be set for the source to be live;
# probing it while unset reports "disabled" rather than red.
#
# Only ``library`` sources remain, since the public backend no longer scrapes
# third-party sites (that moved to the private crimson-sources package, see
# New_System.md). The ``scrape`` category and CANARY stay for the contract and any
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
