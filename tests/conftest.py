"""Shared pytest config.

The suite covers the *pure* logic (parsing, crypto, signing, SSRF classification)
that breaks silently when an upstream rotates its markup or a refactor slips —
none of it touches the network or a database, so there is no fixture wiring here
beyond making the repo root importable.
"""

import os
import sys

# Make `resolvers`, `core`, … importable when pytest is invoked from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Deterministic secrets so HMAC signing tests don't depend on the host env.
os.environ.setdefault("VOE_PROXY_SECRET", "test-voe-secret")
os.environ.setdefault("PROXY_SECRET", "test-proxy-secret")
# Importing api.py runs Config.validate(), which requires TMDB_API_KEY to be set.
# The import-smoke + contract tests never make TMDB calls, so a placeholder is
# enough (locally this comes from a gitignored .env; CI has none). setdefault so
# a real key in the environment still wins.
os.environ.setdefault("TMDB_API_KEY", "test-tmdb-key")
