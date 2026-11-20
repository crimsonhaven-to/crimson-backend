"""Starting chats and managing groups."""

from messaging_engine.db import store

from .conftest import AMI, BO, RAY, room_row


def test_a_direct_chat_opens_by_handle(client, monkeypatch):
    opened = []
    monkeypatch.setattr(store, "direct_room", lambda a, b: opened.append((a, b)) or 10)
    monkeypatch.setattr(store, "rooms", lambda user_id, room_id=None: [room_row()])
    res = client.post("/messages/direct", json={"handle": "@AMI"})
    assert res.status_code == 200
    assert opened == [(1, 2)]
    assert res.json()["members"] == [
        {"handle": "ray", "username": "Raymond"}, {"handle": "ami", "username": None},
    ]


def test_messaging_needs_a_handle(client, caller):
    caller["handle"] = None
    res = client.post("/messages/direct", json={"handle": "ami"})
    assert res.status_code == 409


def test_an_unknown_handle_is_named_in_the_error(client):
    res = client.post("/messages/direct", json={"handle": "ghost"})
    assert res.status_code == 404
    assert "@ghost" in res.json()["detail"]


def test_you_cannot_message_yourself(client):
    assert client.post("/messages/direct", json={"handle": "ray"}).status_code == 400


def test_a_member_who_blocked_you_cannot_be_messaged(client, monkeypatch):
    monkeypatch.setattr(store, "blocked_by_any", lambda user_id, others: [2])
    res = client.post("/messages/direct", json={"handle": "ami"})
    assert res.status_code == 403
    assert "not accepting" in res.json()["detail"]


def test_someone_you_blocked_must_be_unblocked_first(client, monkeypatch):
    monkeypatch.setattr(store, "has_blocked", lambda user_id, other_id: user_id == 1)
    res = client.post("/messages/direct", json={"handle": "ami"})
    assert res.status_code == 403
    assert "You blocked" in res.json()["detail"]


def test_a_group_holds_its_creator_and_the_named_members(client, monkeypatch):
    created = []
    monkeypatch.setattr(store, "create_group", lambda name, ids: created.append((name, ids)) or 11)
    monkeypatch.setattr(
        store, "rooms",
        lambda user_id, room_id=None: [room_row(11, "group", (RAY, AMI, BO), "Gang")],
    )
    res = client.post("/messages/groups", json={"name": " Gang ", "handles": ["ami", "bo", "ami", "ray"]})
    assert res.status_code == 200
    assert created == [("Gang", [1, 2, 3])]


def test_a_group_needs_a_name_and_someone_else(client):
    assert client.post("/messages/groups", json={"name": " ", "handles": ["ami"]}).status_code == 400
    assert client.post("/messages/groups", json={"name": "Me", "handles": ["ray"]}).status_code == 400


def test_someone_elses_room_is_404(client, monkeypatch):
    monkeypatch.setattr(store, "rooms", lambda user_id, room_id=None: [])
    assert client.get("/messages/rooms/99").status_code == 404
    assert client.delete("/messages/rooms/99/membership").status_code == 404


def test_a_direct_chat_cannot_be_renamed_or_left(client, monkeypatch):
    monkeypatch.setattr(store, "rooms", lambda user_id, room_id=None: [room_row()])
    assert client.patch("/messages/rooms/10", json={"name": "x"}).status_code == 400
    assert client.delete("/messages/rooms/10/membership").status_code == 400


def test_adding_members_skips_those_already_in(client, monkeypatch):
    added = []
    monkeypatch.setattr(
        store, "rooms", lambda user_id, room_id=None: [room_row(11, "group", (RAY, AMI), "Gang")]
    )
    monkeypatch.setattr(store, "add_members", lambda room_id, ids: added.append(ids))
    assert client.post("/messages/rooms/11/members", json={"handles": ["ami", "bo"]}).status_code == 200
    assert added == [[3]]


def test_a_group_is_capped(client, monkeypatch):
    crowd = [{"user_id": 100 + i, "handle": f"m{i}", "username": None} for i in range(32)]
    monkeypatch.setattr(
        store, "rooms", lambda user_id, room_id=None: [room_row(11, "group", crowd, "Big")]
    )
    assert client.post("/messages/rooms/11/members", json={"handles": ["bo"]}).status_code == 400


def test_leaving_a_group(client, monkeypatch):
    left = []
    monkeypatch.setattr(
        store, "rooms", lambda user_id, room_id=None: [room_row(11, "group", (RAY, AMI), "Gang")]
    )
    monkeypatch.setattr(store, "leave", lambda room_id, user_id: left.append((room_id, user_id)))
    assert client.delete("/messages/rooms/11/membership").status_code == 200
    assert left == [(11, 1)]
