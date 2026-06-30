# scrapers/__init__.py
#
# The public Crimson backend does NOT scrape third-party streaming sites. That
# logic was moved into the private ``crimson-sources`` package and now runs in
# the client / browser extension / crimson-proxy (see ``New_System.md``).
#
# What remains here are only operator-owned sources — media the server operator
# controls, which is not third-party scraping:
#   * CacheScraper    — episodes this server already remuxed onto your NAS
#   * LocalScraper    — your own registered directories / NAS bind-mounts
#   * JellyfinScraper — your own self-hosted Jellyfin server (env-configured)
# See ``template_scraper.py`` for a documented, inert reference implementation of
# the scraper contract (kept as a file only; not imported/registered here).
from .jellyfin_scraper import JellyfinScraper
from .local_scraper import LocalScraper
from .cache_scraper import CacheScraper
# from .template_scraper import TemplateScraper  # reference only — re-enable with the list entry below.

ALL_SCRAPERS = [
    CacheScraper,      # Server-side video cache: surfaces already-downloaded episodes first.
    LocalScraper,      # Admin-registered local directories / NAS mounts (direct play only).
    JellyfinScraper,   # Your own self-hosted Jellyfin server (env-gated on JELLYFIN_*).
    # TemplateScraper,   # Inert reference implementation of the scraper contract.
]

# --- optional build-time source overlay ------------------------------------
# An operator build may drop extra scraper modules into this package; they're
# auto-discovered and appended here, so this registry needs no edit. A build without
# the overlay has none, so nothing is added. ``showbox_scraper`` is excluded
# (operator-only, wired into /resolve, not /watch). Off via PRIVATE_SOURCES_ENABLED=0.
import sys as _sys

from core.private_sources import discover_private_sources

from .base_scraper import BaseAnimeScraper

_PUBLIC_SCRAPER_MODULES = {
    "base_scraper", "local_scraper", "cache_scraper", "jellyfin_scraper",
    "template_scraper", "showbox_scraper",
}
ALL_SCRAPERS += discover_private_sources(
    _sys.modules[__name__], BaseAnimeScraper, _PUBLIC_SCRAPER_MODULES
)
