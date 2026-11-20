"""Handles as typed resolve to members."""

import pytest
from fastapi import HTTPException

from messaging_engine import handles


def test_normalize_drops_the_at_and_case():
    assert handles.normalize("  @Ray_9 ") == "ray_9"


def test_resolve_keeps_order_and_drops_repeats(monkeypatch):
    rows = [{"user_id": 2, "handle": "b"}, {"user_id": 1, "handle": "a"}]
    monkeypatch.setattr(handles.store, "members_by_handles", lambda wanted: rows)
    assert [r["user_id"] for r in handles.resolve(["A", "b", "@a"])] == [1, 2]


def test_resolve_names_the_missing_handle(monkeypatch):
    monkeypatch.setattr(handles.store, "members_by_handles", lambda wanted: [])
    with pytest.raises(HTTPException) as err:
        handles.resolve(["zed"])
    assert err.value.status_code == 404
    assert "@zed" in err.value.detail
