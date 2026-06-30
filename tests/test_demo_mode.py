"""
DEMO_MODE signup gating: a demo deployment opens registration by bypassing the
invite gate, while a normal deployment still enforces it. Pure logic — the invite
check is exercised with Config + the store method monkeypatched, so no DB is needed
(matching the suite's no-fixtures philosophy).
"""

import pytest
from fastapi import HTTPException

from core.config import Config
from account_engine import routes


def test_demo_mode_bypasses_the_invite_gate(monkeypatch):
    monkeypatch.setattr(Config, "DEMO_MODE", True)
    # Any code — including an empty one — is accepted, and returned as "static" so
    # _consume_invite_code is a no-op (there's no single-use token to burn).
    assert routes._check_invite_code("") is True
    assert routes._check_invite_code("whatever") is True


def test_invite_gate_enforced_when_not_demo(monkeypatch):
    monkeypatch.setattr(Config, "DEMO_MODE", False)
    monkeypatch.setattr(routes, "_allowed_invite_codes", lambda: {"goodcode"})
    # A shared static code is accepted (and flagged static).
    assert routes._check_invite_code("goodcode") is True

    # An unknown code falls through to the single-use token check; with none
    # available it must be rejected (403) rather than silently allowed.
    monkeypatch.setattr(routes.store, "invite_token_is_available", lambda code: False)
    with pytest.raises(HTTPException) as exc:
        routes._check_invite_code("badcode")
    assert exc.value.status_code == 403
