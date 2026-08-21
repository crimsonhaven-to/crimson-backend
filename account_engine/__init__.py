"""
Account engine: sign-in, favorites and watch progress.

Two identity paths. A mnemonic account is an Ed25519 public key derived
client-side from a 12-word BIP39 mnemonic, proven by signing a one-time
challenge (see .ed25519). An email account uses a PBKDF2 password hash (see
.passwords). api.py mounts ``router`` and calls ``store.init_db()`` at startup.
"""

from .db import AccountStore
from .routes import router, store

__all__ = ["router", "store", "AccountStore"]
