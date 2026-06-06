"""
Account engine — mnemonic-based (Ed25519) sign-in, favorites and watch progress.

No usernames, no passwords. An account is an Ed25519 public key derived from a
12-word BIP39 mnemonic held entirely on the client; the server only verifies
signatures over one-time challenges (see account_engine.routes / .ed25519).

Public surface:
    from account_engine import router, store
api.py mounts ``router`` and calls ``store.init_db()`` at startup.
"""

from .db import AccountStore
from .routes import router, store

__all__ = ["router", "store", "AccountStore"]
