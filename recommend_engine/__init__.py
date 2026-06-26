"""
Recommendation engine — "what to watch next" from genres already in the database.

Purely additive and read-only: it derives suggestions from the genres on
``anime_entries`` plus the account engine's favorites / watch progress (see
recommend_engine.recommender). No schema changes, no external API calls.

Public surface:
    from recommend_engine import router
api.py mounts ``router`` alongside the other engine routers.
"""

from .routes import router

__all__ = ["router"]
