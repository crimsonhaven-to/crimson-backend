"""Resolvers for the operator-owned sources, plus any the build-time overlay adds.

Third-party sources live in the private ``crimson-sources`` package and run on
the client. An overlay module that declares ``RESOLVE_ONLY`` delivers its bytes
off-backend and is wired through ``discover_resolve_grants`` instead of here.
"""

import sys as _sys

from core.private_sources import discover_private_sources

from .base_resolver import BaseResolver
from .cache import CacheResolver
from .jellyfin import JellyfinResolver
from .local import LocalResolver

ALL_RESOLVERS = [CacheResolver, LocalResolver, JellyfinResolver]

_PUBLIC_RESOLVER_MODULES = {"base_resolver", "local", "cache", "jellyfin"}
ALL_RESOLVERS += discover_private_sources(
    _sys.modules[__name__], BaseResolver, _PUBLIC_RESOLVER_MODULES
)
