"""HMAC signatures for the signed same-origin proxy links.

Every signed link must verify on whichever replica serves the next request, so
the secret has to be stable and shared: ``PROXY_SECRET``, or a per-source
override. Without either, a random per-process secret keeps a single instance
working, but its links die on restart and fail on every other replica.
"""

import hashlib
import hmac
import logging
import os

from core.config import get_settings

logger = logging.getLogger(__name__)


def resolve_secret(specific_env: str) -> bytes:
    """``PROXY_SECRET``, else ``specific_env``, else a random per-process secret.

    ``specific_env`` is read straight from the environment because overlay
    modules pass names the public settings do not declare."""
    value = get_settings().proxy_secret or os.getenv(specific_env)
    if value:
        return value.encode("utf-8")
    logger.warning(
        "%s/PROXY_SECRET not set, so signed links use a random per-process secret. "
        "They break on restart and do not verify across replicas.",
        specific_env,
    )
    return os.urandom(32).hex().encode("utf-8")


def sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify(secret: bytes, payload: str, signature: str) -> bool:
    return bool(signature) and hmac.compare_digest(sign(secret, payload), signature)
