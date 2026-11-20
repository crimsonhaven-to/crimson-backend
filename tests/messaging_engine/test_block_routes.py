"""Finding members and blocking them."""

from messaging_engine.db import store


def test_finding_a_member_says_if_you_blocked_them(client, monkeypatch):
    monkeypatch.setattr(store, "has_blocked", lambda user_id, other_id: other_id == 2)
    assert client.get("/messages/members/Ami").json() == {
        "handle": "ami", "username": None, "blocked": True,
    }
    assert client.get("/messages/members/nobody").status_code == 404


def test_blocking_and_unblocking(client, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "block", lambda user_id, other: calls.append(("block", other)))
    monkeypatch.setattr(store, "unblock", lambda user_id, other: calls.append(("unblock", other)))
    assert client.put("/messages/blocks/ami").status_code == 200
    assert client.delete("/messages/blocks/ami").status_code == 200
    assert client.put("/messages/blocks/ray").status_code == 400
    assert calls == [("block", 2), ("unblock", 2)]
