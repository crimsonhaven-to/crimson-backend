# resolvers/__init__.py
#
# Resolvers turn a scraper's embed marker into a playable stream URL. With the
# third-party sources removed (their scraping/resolving now lives in the private
# ``crimson-sources`` package — see ``New_System.md``), only the operator-owned
# resolvers remain. See ``template.py`` for a documented, inert reference
# implementation of the resolver contract (kept as a file only; not registered).
from .jellyfin import JellyfinResolver
from .local import LocalResolver
from .cache import CacheResolver
# from .template import TemplateResolver  # reference only — re-enable with the list entry below.

# The unified list of all resolvers. ``resolve_streams`` (api.py) matches an
# embed to the first resolver whose ``domain_keyword`` is a substring of it.
ALL_RESOLVERS = [
    CacheResolver,     # server-side cache -> /cache_proxy (direct play); labelled per NAS target
    LocalResolver,     # admin-registered local dirs / NAS mounts -> /local_proxy (direct play)
    JellyfinResolver,  # your own Jellyfin server -> token-injecting /jellyfin_proxy
    # TemplateResolver,  # inert reference implementation of the resolver contract
]
