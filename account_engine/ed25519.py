"""
Pure-Python Ed25519 (RFC 8032) — no native dependencies.

Why vendored: the deploy image is ``python:3.14-slim`` with only ``gcc`` (no
Rust), so ``cryptography`` can't be guaranteed to build and ``PyNaCl`` adds a
libsodium build step — both fragile on a bleeding-edge CPython. Account auth
needs exactly one primitive server-side: **verify an Ed25519 signature against a
public key**. That's small and stable enough to vendor the canonical RFC 8032
reference (using the C-accelerated built-in ``pow`` for the field arithmetic so
it's fast enough — a verify is a handful of milliseconds, and logins are rare).

The server only ever calls :func:`verify`. :func:`public_key_from_seed` and
:func:`sign` are the client side of the contract — they exist so the test suite
can act as a client, and as the executable spec the frontend must match:

    seed32  = <32 bytes>                      # BIP39: first 32 bytes of the seed
    pubkey  = public_key_from_seed(seed32)    # == @noble/ed25519 getPublicKey
    sig     = sign(message, seed32)           # == @noble/ed25519 sign

See ``account_engine`` package docs / README for the full client derivation
spec (BIP39 mnemonic -> seed -> seed[:32] -> this).
"""

import hashlib

# Curve / field constants (Ed25519).
_b = 256
_q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    # Fermat inverse; built-in pow with a modulus is C-fast.
    return pow(x, _q - 2, _q)


_d = (-121665 * _inv(121666)) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = (4 * _inv(5)) % _q
_Bx = _xrecover(_By)
_B = (_Bx % _q, _By % _q)


def _edwards_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    denom = _inv(1 + _d * x1 * x2 * y1 * y2)
    x3 = (x1 * y2 + x2 * y1) * denom % _q
    denom2 = _inv(1 - _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * denom2 % _q
    return (x3, y3)


def _scalarmult(P, e: int):
    """Double-and-add (iterative, so no recursion-depth limits)."""
    Q = (0, 1)  # neutral element
    while e > 0:
        if e & 1:
            Q = _edwards_add(Q, P)
        P = _edwards_add(P, P)
        e >>= 1
    return Q


def _encodeint(y: int) -> bytes:
    return y.to_bytes(_b // 8, "little")


def _encodepoint(P) -> bytes:
    x, y = P
    val = y | ((x & 1) << (_b - 1))
    return val.to_bytes(_b // 8, "little")


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _clamp_scalar(h: bytes) -> int:
    """The RFC 8032 secret scalar derived from the lower half of SHA512(seed)."""
    return 2 ** (_b - 2) + sum(2 ** i * _bit(h, i) for i in range(3, _b - 2))


def _Hint(m: bytes) -> int:
    h = _H(m)
    return sum(2 ** i * _bit(h, i) for i in range(2 * _b))


def public_key_from_seed(seed: bytes) -> bytes:
    """Derive the 32-byte Ed25519 public key from a 32-byte seed (private key)."""
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = _H(seed)
    a = _clamp_scalar(h)
    A = _scalarmult(_B, a)
    return _encodepoint(A)


def sign(message: bytes, seed: bytes) -> bytes:
    """Produce a 64-byte Ed25519 signature over ``message`` (client/test side)."""
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = _H(seed)
    a = _clamp_scalar(h)
    pk = _encodepoint(_scalarmult(_B, a))
    r = _Hint(h[_b // 8:_b // 4] + message)
    R = _scalarmult(_B, r)
    S = (r + _Hint(_encodepoint(R) + pk + message) * a) % _L
    return _encodepoint(R) + _encodeint(S)


def _isoncurve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decodeint(s: bytes) -> int:
    return int.from_bytes(s, "little")


def _decodepoint(s: bytes):
    y = int.from_bytes(s, "little") & ((1 << (_b - 1)) - 1)
    x = _xrecover(y)
    if (x & 1) != _bit(s, _b - 1):
        x = _q - x
    P = (x, y)
    if not _isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a 64-byte Ed25519 signature. Returns False on any malformed input
    rather than raising, so callers can treat it as a plain boolean check."""
    try:
        if len(signature) != 64 or len(public_key) != 32:
            return False
        R = _decodepoint(signature[:32])
        A = _decodepoint(public_key)
        S = _decodeint(signature[32:])
        if S >= _L:
            return False
        h = _Hint(signature[:32] + public_key + message)
        # Cofactorless check: [S]B == R + [h]A
        return _scalarmult(_B, S) == _edwards_add(R, _scalarmult(A, h))
    except (ValueError, IndexError):
        return False
