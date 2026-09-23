"""Scrapers for the operator-owned sources, plus any the build-time overlay adds.

The backend does not scrape third-party sites: that lives in the private
``crimson-sources`` package and runs on the client. What stays here is media the
operator controls. An overlay module that declares ``RESOLVE_ONLY`` is wired
through its resolver's ``RESOLVE_GRANT`` instead of here.
"""

import sys as _sys

from core.private_sources import discover_private_sources

from .base_scraper import BaseAnimeScraper
from .cache_scraper import CacheScraper
from .jellyfin_scraper import JellyfinScraper
from .local_scraper import LocalScraper

ALL_SCRAPERS = [CacheScraper, LocalScraper, JellyfinScraper]

_PUBLIC_SCRAPER_MODULES = {"base_scraper", "local_scraper", "cache_scraper", "jellyfin_scraper"}
ALL_SCRAPERS += discover_private_sources(
    _sys.modules[__name__], BaseAnimeScraper, _PUBLIC_SCRAPER_MODULES
)
