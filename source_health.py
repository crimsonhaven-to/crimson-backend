"""
Source-health metadata — the static descriptors behind the admin dashboard's
"Source Health" view. The actual probing lives in api.py (it needs the scraper
pipeline + resolvers), but the canary title, per-source labels/categories and
env-gates are declared here so the probe stays a thin loop over this table.

Two categories:

* ``scrape``  — an external streaming site we scrape. Health = can it still find
  + surface embeds for a known canary title? (green ok / yellow empty / red error.)
* ``library`` — an operator-provided source (the server cache, local NAS dirs,
  a personal Jellyfin). It only holds what the operator added, so probing it with
  a fixed canary is meaningless; health = is it configured and does it hold
  anything (green active / grey idle / dim disabled).

Keyed by scraper class ``__name__`` so the probe can look a source up by class.
"""

from __future__ import annotations

import os

# The canary: a famous title chosen to exist on as many sources as possible and
# to carry BOTH an AniList and a TMDB mapping, so the anime, general and
# TMDB-keyed scrapers can all attempt it. Defaults to Attack on Titan (S1E1);
# override via env for a different probe target.
CANARY = {
    "title": os.getenv("HEALTH_CANARY_TITLE", "Attack on Titan"),
    "tmdb_id": int(os.getenv("HEALTH_CANARY_TMDB", "1429")),       # AoT (TMDB tv)
    "season": int(os.getenv("HEALTH_CANARY_SEASON", "1")),
    "episode": int(os.getenv("HEALTH_CANARY_EPISODE", "1")),
    "anilist_id": int(os.getenv("HEALTH_CANARY_ANILIST", "16498")),  # AoT (AniList)
}

# Per-source descriptors. ``env_gate`` names an env var that must be set for the
# source to be live (probing it while unset reports "disabled" rather than red).
SOURCE_META = {
    # --- external scrape sources -------------------------------------------
    "GogoScraper":     {"label": "GogoAnime",   "category": "scrape", "note": "Anime · GogoAnime API"},
    "AniworldScraper": {"label": "AniWorld",     "category": "scrape", "note": "Anime · German (VOE/Vidmoly)"},
    "StoScraper":      {"label": "s.to",         "category": "scrape", "note": "TV + anime · German (VOE/Vidmoly)"},
    "StoMirrorScraper":{"label": "s.to (mirror)","category": "scrape", "note": "Turnstile-free IP mirror of s.to"},
    "AniwatchScraper": {"label": "AniWatch",     "category": "scrape", "note": "Anime · VidSrc/megaplay"},
    "MovishScraper":   {"label": "Movish",       "category": "scrape", "note": "TMDB-keyed · ad-free player"},
    "PlayimdbScraper": {"label": "PlayIMDb",     "category": "scrape", "note": "TMDB-keyed · VidAPI chain"},
    "AnimekaiScraper": {"label": "AnimeKai",     "category": "scrape", "note": "Anime · 1anime API"},
    "AnimeSugeScraper":{"label": "AnimeSuge",    "category": "scrape", "note": "Anime · ad-free Kiranime"},
    "CinemabzScraper": {"label": "Cinema.bz",    "category": "scrape", "note": "TMDB-keyed · 3-provider HLS"},
    "ShowBoxScraper":  {"label": "ShowBox",      "category": "scrape", "note": "Direct-file · FebBox",
                        "env_gate": "FEBBOX_UI_TOKEN"},
    # --- operator-provided library sources ---------------------------------
    "CacheScraper":    {"label": "Server Cache", "category": "library", "note": "Remuxed episodes on your NAS"},
    "LocalScraper":    {"label": "Local Media",  "category": "library", "note": "Registered NAS / bind-mount dirs"},
    "JellyfinScraper": {"label": "Jellyfin",     "category": "library", "note": "Your personal Jellyfin server"},
}


def meta_for(class_name: str) -> dict:
    """Descriptor for a scraper class name, with safe defaults for an unlisted one."""
    return SOURCE_META.get(class_name, {"label": class_name, "category": "scrape", "note": None})
