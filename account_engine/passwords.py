"""
Password hashing for the email+password sign-in path.

PBKDF2-HMAC-SHA256 via stdlib ``hashlib``, because the deploy image has no
compiler toolchain for ``argon2-cffi`` / ``bcrypt`` (same reason ed25519 is
vendored). Hashes are self-describing (``algo$iterations$salt$hash``) so the
iteration count can be raised without invalidating existing rows, with
``needs_rehash`` flagging older ones at next login.

A hash costs roughly 0.2 to 0.4s, so callers run these in a threadpool.
"""

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
# OWASP 2023 floor. Stored hashes carry their own count, so raising this only
# triggers a transparent rehash at next login.
ITERATIONS = 600_000
SALT_BYTES = 16

# Bounded so an absurdly long password can't become a CPU-DoS vector.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Return a self-describing ``pbkdf2_sha256$iters$salt$hash`` string."""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check against a stored hash. A malformed hash returns False."""
    if not encoded:
        return False
    try:
        algorithm, iters_s, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        iterations = int(iters_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def needs_rehash(encoded: str) -> bool:
    """True if the hash is outdated and should be re-hashed. Call after a
    successful ``verify_password``."""
    try:
        algorithm, iters_s, _, _ = encoded.split("$")
        return algorithm != ALGORITHM or int(iters_s) < ITERATIONS
    except (ValueError, AttributeError):
        return True
