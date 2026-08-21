"""
Shared slowapi rate limiter for the abuse-prone endpoints.

Its own module so api.py and account_engine.routes get the *same* ``Limiter``
without a circular import.

Keyed on client IP. uvicorn runs with ``--proxy-headers``, so that is the real
X-Forwarded-For client rather than the proxy's address.

Storage is in-memory per replica by default: a dependency-free baseline that
already blunts the two real abuse vectors, hammering the expensive /watch fan-out
and flooding /auth/challenge to grow the challenges table. For exact limits
across a Swarm, point ``RATE_LIMIT_STORAGE_URI`` at a shared Redis.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# headers_enabled stays False: slowapi can only inject X-RateLimit-* headers if
# every decorated endpoint declares a ``response: Response`` parameter, and
# without that it raises at request time. The 429 still fires either way.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATE_LIMIT_STORAGE_URI", "memory://"),
    headers_enabled=False,
)
