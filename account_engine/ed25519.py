"""Ed25519 signature verification (RFC 8032), vendored in pure Python.

The deploy image is python:3.14-slim with no Rust toolchain, so cryptography and
PyNaCl are fragile to build there, and the server needs only one primitive.
Field arithmetic goes through the built-in ``pow``, so a verify costs a few
milliseconds. Signing happens in the browser (@noble/ed25519).
"""

import hashlib

_q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
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
_B = (_xrecover(_By) % _q, _By)


def _edwards_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2) % _q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2) % _q
    return (x3, y3)


def _scalarmult(P, e: int):
    """Double-and-add, iterative to avoid recursion limits."""
    Q = (0, 1)
    while e > 0:
        if e & 1:
            Q = _edwards_add(Q, P)
        P = _edwards_add(P, P)
        e >>= 1
    return Q


def _hash_int(m: bytes) -> int:
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def _is_on_curve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decode_point(s: bytes):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != s[31] >> 7:
        x = _q - x
    P = (x, y)
    if not _is_on_curve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a 64-byte signature. False on malformed input rather than raising."""
    try:
        if len(signature) != 64 or len(public_key) != 32:
            return False
        R = _decode_point(signature[:32])
        A = _decode_point(public_key)
        S = int.from_bytes(signature[32:], "little")
        if S >= _L:
            return False
        h = _hash_int(signature[:32] + public_key + message)
        # Cofactorless check: [S]B == R + [h]A
        return _scalarmult(_B, S) == _edwards_add(R, _scalarmult(A, h))
    except (ValueError, IndexError):
        return False
