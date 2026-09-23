"""The vendored verifier against the RFC 8032 section 7.1 test vectors."""


import pytest

from account_engine import ed25519


VECTORS = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bac"
        "c61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e"
        "458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


@pytest.mark.parametrize("public_key,message,signature", VECTORS)
def test_rfc8032_vectors_verify(public_key, message, signature):
    assert ed25519.verify(bytes.fromhex(public_key), bytes.fromhex(message), bytes.fromhex(signature))


@pytest.mark.parametrize("public_key,message,signature", VECTORS)
def test_a_tampered_message_fails(public_key, message, signature):
    assert not ed25519.verify(bytes.fromhex(public_key), b"tampered", bytes.fromhex(signature))


def test_a_signature_from_another_key_fails():
    (pk1, _, _), (_, msg2, sig2) = VECTORS
    assert not ed25519.verify(bytes.fromhex(pk1), bytes.fromhex(msg2), bytes.fromhex(sig2))


def test_malformed_input_is_false_not_an_error():
    pk, msg, sig = VECTORS[0]
    assert not ed25519.verify(bytes.fromhex(pk), b"", bytes.fromhex(sig)[:63])
    assert not ed25519.verify(b"\x00" * 31, b"", bytes.fromhex(sig))
    assert not ed25519.verify(bytes.fromhex(pk), b"", b"\xff" * 64)
