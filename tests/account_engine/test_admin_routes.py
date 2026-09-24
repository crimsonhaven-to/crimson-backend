"""Per-user feature grants on PATCH /admin/users/{id}: each change is written
and audited, and an unchanged flag writes nothing."""

import pytest

from account_engine import admin_routes
from account_engine.schemas import UserUpdate


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(
        admin_routes.music_store, "set_music_access", lambda uid, on: calls.append(("music", uid, on))
    )
    monkeypatch.setattr(
        admin_routes.chat_store, "set_chat_access", lambda uid, on: calls.append(("chat", uid, on))
    )
    monkeypatch.setattr(
        admin_routes, "log_admin_action", lambda req, admin, action, **kw: calls.append(action)
    )
    return calls


def _apply(target, **changes):
    admin_routes._apply_user_update(None, {"user_id": 1}, 2, target, UserUpdate(**changes))


def test_music_and_lumi_grants_are_written_and_audited(recorded):
    _apply({"email": "a@b"}, music_enabled=True, chat_enabled=True)
    assert ("music", 2, True) in recorded and "music_granted" in recorded
    assert ("chat", 2, True) in recorded and "chat_granted" in recorded


def test_revoking_music_is_audited(recorded):
    _apply({"music_enabled": True}, music_enabled=False)
    assert recorded == [("music", 2, False), "music_revoked"]


def test_an_unchanged_grant_writes_nothing(recorded):
    _apply({"music_enabled": True}, music_enabled=True)
    assert recorded == []
