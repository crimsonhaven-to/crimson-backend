"""
Supporters engine — Ko-fi webhook ingest + the public "Lumi's Loved Mortals" list.

Ko-fi has no queryable supporters API: it only pushes a webhook on each payment
event (and never on cancellation). This engine receives those webhooks into an
append-only ledger and derives the public supporter list by aggregating it (see
supporters_engine.db / .routes).

Public surface:
    from supporters_engine import router, store
api.py mounts ``router`` and calls ``store.init_db()`` at startup — mirroring the
account engine.
"""

from .db import SupporterStore
from .routes import router, store

__all__ = ["router", "store", "SupporterStore"]
