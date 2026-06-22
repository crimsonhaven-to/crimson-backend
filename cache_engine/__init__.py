# cache_engine — server-side video cache. When caching is enabled, playing an
# episode kicks off a background job that downloads the COMPLETE stream to an
# admin-registered NAS target (remuxing HLS -> mp4 with ffmpeg) and records it in
# the DB; on the next play the CacheScraper surfaces that file as a first-class
# source, labelled with the target's admin-given name and the original language.
#
# The DB store lives in .db (cache_targets / cache_settings / cached_episodes);
# filesystem helpers (token <-> path, the /cache_proxy safety check, NAS path
# planning, the admin probe) live in .fs; the background download manager lives
# in .downloader. The scraper/resolver that plug into the watch pipeline live
# under scrapers/ and resolvers/ and import from here.
from .db import CacheStore

__all__ = ["CacheStore"]
