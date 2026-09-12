"""Airing calendar, per-title subscriptions and "a new episode aired" email.

``metadata_engine.anilist`` has asked AniList for ``nextAiringEpisode`` on every
title fetch since the beginning and discarded it. This engine is the first thing
that keeps it.

  * ``db.py``        the schedule, subscriptions and the notification ledger
  * ``schedule.py``  the windowed AniList fetch (one window, not one per follow)
  * ``notifier.py``  the two scheduled jobs: refresh, then claim and send
  * ``routes.py``    ``/calendar`` and ``/account/subscriptions``

The calendar and the subscription surface are always available. Only the part
that mails a human is gated, by ``AIRING_NOTIFY_ENABLED``, default off: it is the
one thing in this backend that cannot be taken back by redeploying.

Schema lives in ``migrations/004_airing.sql``; there is no ``init_db()`` here,
matching ``chat_engine``.
"""

from .db import AiringStore, store
from .routes import router

__all__ = ["router", "store", "AiringStore"]
