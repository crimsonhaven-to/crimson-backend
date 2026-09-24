"""The music grant: 503 without a library, 403 without the account's grant."""

import pytest
from fastapi import HTTPException

from music_engine.access import require_music_user


async def test_no_library_is_503(monkeypatch):
    monkeypatch.delenv("MUSIC_ROOT", raising=False)
    with pytest.raises(HTTPException) as err:
        await require_music_user({"music_enabled": True})
    assert err.value.status_code == 503


async def test_no_grant_is_403(monkeypatch):
    monkeypatch.setenv("MUSIC_ROOT", "/music")
    with pytest.raises(HTTPException) as err:
        await require_music_user({"music_enabled": False})
    assert err.value.status_code == 403


async def test_a_granted_account_passes(monkeypatch):
    monkeypatch.setenv("MUSIC_ROOT", "/music")
    user = {"music_enabled": True}
    assert await require_music_user(user) is user
