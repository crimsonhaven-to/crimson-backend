"""Account ids never leave the server."""

from messaging_engine.payloads import message_payload, room_payload

from .conftest import NOW, room_row


def test_a_room_lists_members_by_handle_only():
    payload = room_payload({
        **room_row(), "last_id": 3, "last_body": "hi", "last_created_at": NOW,
        "last_mine": False, "last_handle": "ami", "last_username": None,
    })
    assert "user_id" not in str(payload)
    assert payload["last_message"]["sender"] == {"handle": "ami", "username": None}


def test_a_former_members_message_has_no_sender():
    payload = message_payload({
        "id": 1, "body": "hi", "attachment": None, "created_at": NOW, "mine": None,
        "sender_handle": None, "sender_username": None,
    })
    assert payload["sender"] is None
    assert payload["mine"] is False
