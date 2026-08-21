"""
Lumi's chatbot engine.

A permission-gated conversational surface where a granted account can talk to the
mascot and have her act on the catalogue: recommend from their watch history,
resolve a title to a playable link, and manage watchlists.

  * ``models.py``    selectable provider models, with pricing and capabilities
  * ``persona.py``   Lumi's system prompt
  * ``tools.py``     tool schemas and their dispatch into the existing engines
  * ``providers.py`` Anthropic and Gemini behind one streaming interface
  * ``db.py``        settings, conversations, usage ledger
  * ``routes.py``    the authed NDJSON chat endpoint

Nothing here owns catalogue data. Every tool is a thin call into code that
already serves the REST API, so the chatbot cannot drift from what the rest of
the backend believes.
"""

from .db import ChatStore
from .routes import router, store

__all__ = ["router", "store", "ChatStore"]
