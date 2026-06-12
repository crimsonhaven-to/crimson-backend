"""
Password hashing for the email+password sign-in path.

Constraints that shaped this:

  * The image is ``python:3.14-slim`` with **no Rust/compiler toolchain**, so the
    usual ``argon2-cffi`` / ``bcrypt`` / ``passlib`` stack (native wheels) is off
    the table — same reason ed25519 is vendored pure-Python here. ``hashlib`` is
    stdlib (OpenSSL-backed) and always present, so we use PBKDF2-HMAC-SHA256.
  * Hashes are stored self-describing (``algo$iterations$salt$hash``, all
    base64) so the iteration count can be raised later without invalidating
    existing rows — ``needs_rehash`` flags older hashes on next login.

PBKDF2 at the OWASP-recommended 600k iterations costs ~0.2–0.4s; callers run
``hash_password`` / ``verify_password`` in a threadpool (see routes) so the
event loop is never blocked.
"""

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
# OWASP 2023 floor for PBKDF2-HMAC-SHA256. Bump deliberately; stored hashes carry
# their own iteration count so a bump only triggers a transparent rehash on login.
ITERATIONS = 600_000
SALT_BYTES = 16

# Sanity bounds so an absurdly long password can't be used as a CPU-DoS vector
# (PBKDF2 cost scales with the input length the HMAC has to chew through).
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
    """Constant-time check of ``password`` against a stored hash. Never raises —
    a malformed/empty stored hash simply returns False."""
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
    """True if a stored hash uses an older algorithm/iteration count and should be
    transparently re-hashed (call after a successful ``verify_password``)."""
    try:
        algorithm, iters_s, _, _ = encoded.split("$")
        return algorithm != ALGORITHM or int(iters_s) < ITERATIONS
    except (ValueError, AttributeError):
        return True
