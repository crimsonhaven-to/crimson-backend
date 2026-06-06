"""
Shared resolution of the HMAC secret used to sign same-origin proxy URLs
(PlayIMDb, AnimeSuge).

Why this exists: those proxies fetch rotating upstream hosts, so each proxied
URL is HMAC-signed and the proxy refuses anything unsigned (closes the
open-relay / SSRF hole). The signing secret therefore has to be **stable across
restarts and identical on every replica** — a link minted by one replica is
verified by whichever replica the player's next request is load-balanced to. A
per-process random secret silently breaks playback under horizontal scaling
(Docker Swarm) with intermittent 403s.

Resolution order:
  1. ``PROXY_SECRET``           — one shared secret for all proxy sources (preferred)
  2. the per-source var, e.g. ``PLAYIMDB_PROXY_SECRET`` / ``ANIMESUGE_PROXY_SECRET``
  3. a random per-process secret — only OK for a single instance; logged loudly
"""

import logging
import os

logger = logging.getLogger(__name__)


def resolve_secret(specific_env: str) -> bytes:
    """Return the signing secret as bytes (see module docstring for the order)."""
    value = os.getenv("PROXY_SECRET") or os.getenv(specific_env)
    if value:
        return value.encode("utf-8")
    logger.warning(
        "%s/PROXY_SECRET not set — using a random per-process proxy secret. "
        "In-flight stream links break on restart and signatures will NOT verify "
        "across replicas. Set PROXY_SECRET for multi-replica / production deploys.",
        specific_env,
    )
    return os.urandom(32).hex().encode("utf-8")
