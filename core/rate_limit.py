"""The slowapi limiter shared by every rate-limited route.

Keyed on client IP, which is the real client because uvicorn runs with
``--proxy-headers``. Storage is in-memory per replica unless
``RATE_LIMIT_STORAGE_URI`` points at a shared Redis, so limits are per replica.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from core.config import get_settings

# slowapi can only add X-RateLimit-* headers to endpoints that declare a
# ``response: Response`` parameter and raises at request time otherwise.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=get_settings().rate_limit_storage_uri,
    headers_enabled=False,
)
