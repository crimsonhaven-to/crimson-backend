"""Reading, sending and the unread badge."""

from messaging_engine import attachments
from messaging_engine.db import store

from .conftest import NOW, room_row


def _sent(monkeypatch):
    sent = []

    def send(room_id, sender_id, body, attachment):
        sent.append((room_id, sender_id, body, attachment))
        return {"id": 5, "created_at": NOW}

    monkeypatch.setattr(store, "rooms", lambda user_id, room_id=None: [room_row()])
    monkeypatch.setattr(store, "send", send)
    return sent


def test_a_text_message_is_trimmed_and_echoed(client, monkeypatch):
    sent = _sent(monkeypatch)
    res = client.post("/messages/rooms/10/messages", json={"body": "  hi  "})
    assert res.status_code == 200
    assert sent == [(10, 1, "hi", None)]
    assert res.json() == {
        "id": 5, "body": "hi", "attachment": None, "created_at": NOW.isoformat(),
        "mine": True, "sender": {"handle": "ray", "username": "Raymond"},
    }


def test_an_empty_message_is_refused(client, monkeypatch):
    _sent(monkeypatch)
    assert client.post("/messages/rooms/10/messages", json={"body": "   "}).status_code == 400


def test_a_show_card_needs_no_text(client, monkeypatch):
    sent = _sent(monkeypatch)
    card = {"kind": "title", "media": "show", "id": 1399, "title": "Thrones", "poster": "https://x/p.jpg"}
    assert client.post("/messages/rooms/10/messages", json={"attachment": card}).status_code == 200
    assert sent[0][3] == card


def test_a_song_no_longer_in_the_library_is_refused(client, monkeypatch):
    _sent(monkeypatch)

    def gone(attachment):
        raise attachments.UnknownSong()

    monkeypatch.setattr(attachments, "snapshot", gone)
    res = client.post("/messages/rooms/10/messages", json={"attachment": {"kind": "song", "token": "7.x"}})
    assert res.status_code == 400


def test_a_blocked_direct_chat_cannot_be_written_to(client, monkeypatch):
    _sent(monkeypatch)
    monkeypatch.setattr(store, "has_blocked", lambda user_id, other_id: user_id == 2)
    res = client.post("/messages/rooms/10/messages", json={"body": "hi"})
    assert res.status_code == 403


def test_reading_messages_needs_membership(client, monkeypatch):
    monkeypatch.setattr(store, "rooms", lambda user_id, room_id=None: [])
    assert client.get("/messages/rooms/10/messages").status_code == 404


def test_polling_passes_the_cursor_through(client, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "rooms", lambda user_id, room_id=None: [room_row()])
    monkeypatch.setattr(
        store, "messages",
        lambda room_id, user_id, after, before, limit: calls.append((after, before, limit)) or [],
    )
    client.get("/messages/rooms/10/messages?after=4")
    client.get("/messages/rooms/10/messages?before=9&limit=20")
    assert calls == [(4, None, 50), (None, 9, 20)]


def test_unread_is_zero_without_a_handle(client, caller, monkeypatch):
    monkeypatch.setattr(store, "unread_total", lambda user_id: 3)
    assert client.get("/messages/unread").json() == {"count": 3}
    caller["handle"] = None
    assert client.get("/messages/unread").json() == {"count": 0}


def test_marking_read(client, monkeypatch):
    marked = []
    monkeypatch.setattr(store, "rooms", lambda user_id, room_id=None: [room_row()])
    monkeypatch.setattr(store, "mark_read", lambda room_id, user_id, last_id: marked.append(last_id))
    assert client.post("/messages/rooms/10/read", json={"last_id": 8}).status_code == 200
    assert marked == [8]
