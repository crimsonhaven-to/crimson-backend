"""
API-key engine — admin-minted machine credentials for the movie-web (`/mw`)
bridge.

These are NOT user accounts: an admin mints a key in the dashboard and bakes it
into the modified movie-web fork's proxy, which injects it server-side on calls
to the ``/mw`` bridge endpoints. The login wall (api.py) accepts a valid
``X-API-Key`` ONLY for ``/mw`` paths, so a key can drive the bridge and nothing
else.

Public surface:
    from apikey_engine import store      # ApiKeyStore singleton
api.py calls ``store.init_db()`` at startup and the admin dashboard mounts the
management routes from account_engine.admin_routes.
"""

from .db import ApiKeyStore, KEY_SCHEME

store = ApiKeyStore()

__all__ = ["store", "ApiKeyStore", "KEY_SCHEME"]
