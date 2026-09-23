"""Signup gating and account deletion. A demo deployment skips the invite gate,
and deleting an account needs the account's own credential, never the bearer
token alone.
"""

import pytest

from fastapi import HTTPException

from account_engine import audit, auth, passwords


from account_engine.schemas import DeleteAccountRequest



from core.config import get_settings



class _Req:
    headers: dict = {}
    client = None


def test_password_account_must_supply_the_right_password(monkeypatch):
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)
    user = {"user_id": 7, "password_hash": passwords.hash_password("correct horse")}
    body = DeleteAccountRequest(password="wrong horse")
    with pytest.raises(HTTPException) as excinfo:
        auth.confirm_owner(user, body, _Req())
    assert excinfo.value.status_code == 401


def test_password_account_passes_with_the_right_password():
    user = {"user_id": 7, "password_hash": passwords.hash_password("correct horse")}
    body = DeleteAccountRequest(password="correct horse")
    auth.confirm_owner(user, body, _Req())


def test_a_bearer_token_alone_never_confirms():
    """The whole point: the credential an attacker can steal is not enough."""
    user = {"user_id": 7, "password_hash": passwords.hash_password("pw")}
    with pytest.raises(HTTPException):
        auth.confirm_owner(user, DeleteAccountRequest(), _Req())


def test_mnemonic_account_needs_a_signature():
    user = {"user_id": 7, "password_hash": None, "public_key": "ab" * 32}
    with pytest.raises(HTTPException) as excinfo:
        auth.confirm_owner(user, DeleteAccountRequest(), _Req())
    assert excinfo.value.status_code == 400


def test_mnemonic_account_verifies_the_signature(monkeypatch):
    seen = {}

    def fake_verify(public_key, challenge, signature, request, flow):
        seen.update(public_key=public_key, challenge=challenge, flow=flow)

    monkeypatch.setattr(auth, "verify_signed_challenge", fake_verify)
    user = {"user_id": 7, "password_hash": None, "public_key": "ab" * 32}
    body = DeleteAccountRequest(challenge="c", signature="s")
    auth.confirm_owner(user, body, _Req())
    assert seen["public_key"] == "ab" * 32 and seen["flow"] == "delete_account"


def test_an_account_with_neither_credential_is_refused():
    user = {"user_id": 7, "password_hash": None, "public_key": None}
    with pytest.raises(HTTPException) as excinfo:
        auth.confirm_owner(user, DeleteAccountRequest(), _Req())
    assert excinfo.value.status_code == 400


def test_demo_mode_bypasses_the_invite_gate(monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_mode", True)
    # Any code, even an empty one, counts as "static", so consume_invite_code has
    # no single-use token to burn.
    assert auth.check_invite_code("") is True
    assert auth.check_invite_code("whatever") is True


def test_invite_gate_enforced_when_not_demo(monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_mode", False)
    monkeypatch.setattr(get_settings(), "signup_invite_code", ["goodcode"])
    # A shared static code is accepted (and flagged static).
    assert auth.check_invite_code("goodcode") is True

    # An unknown code falls through to the single-use token check; with none
    # available it must be rejected (403) rather than silently allowed.
    monkeypatch.setattr(auth.store, "invite_token_is_available", lambda code: False)
    with pytest.raises(HTTPException) as exc:
        auth.check_invite_code("badcode")
    assert exc.value.status_code == 403
