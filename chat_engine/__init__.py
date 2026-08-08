"""
Lumi's chatbot engine.

A small, permission-gated conversational surface that lets a granted account talk
to the mascot and have her act on the catalogue: recommend from their own watch
history, resolve a title to a playable deep link, check where they left off, and
manage watchlists.

Layout mirrors the other engines:

  * ``models.py``    - the catalogue of selectable provider models and pricing.
  * ``persona.py``   - Lumi's system prompt.
  * ``tools.py``     - tool schemas plus the dispatch into the existing engines.
  * ``providers.py`` - Anthropic and Gemini behind one streaming interface.
  * ``db.py``        - settings, conversations, usage ledger.
  * ``routes.py``    - the authed NDJSON chat endpoint.

Nothing here owns catalogue data. Every tool is a thin call into code that
already exists (recommend_engine, account_engine, the web discovery routes), so
the chatbot cannot drift away from what the rest of the backend believes.

Public surface:
    from chat_engine import router, store
api.py mounts ``router`` alongside the other engine routers and schedules
``store.prune`` on the maintenance replica.
"""

from .db import ChatStore
from .routes import router, store

__all__ = ["router", "store", "ChatStore"]
