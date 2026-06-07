"""
Shared rate limiter (slowapi) for the abuse-prone endpoints.

Lives in its own tiny module so both ``api.py`` and ``account_engine.routes`` can
import the *same* ``Limiter`` instance without a circular import (api.py already
imports the account router).

Keyed on the client IP. Behind our reverse proxy uvicorn runs with
``--proxy-headers --forwarded-allow-ips=*``, so ``request.client.host`` reflects
the real ``X-Forwarded-For`` client rather than the proxy's address — per-user
limiting therefore works in production.

Storage is in-memory **per replica** by default — a deliberate, dependency-free
baseline that already blunts the two real abuse vectors: hammering the expensive
``/watch`` scraper fan-out (turning us into a scraping/DoS amplifier) and flooding
``/auth/challenge`` to grow the challenges table. For exact global limits across a
multi-replica Swarm, point slowapi at a shared Redis via ``RATE_LIMIT_STORAGE_URI``
(e.g. ``redis://redis:6379``).
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# headers_enabled stays False: slowapi can only inject X-RateLimit-* headers when
# every decorated endpoint also declares a ``response: Response`` parameter, and
# turning it on without that raises at request time. Enforcement (the 429 with
# Retry-After) works regardless; we just don't advertise the running counter.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATE_LIMIT_STORAGE_URI", "memory://"),
    headers_enabled=False,
)
