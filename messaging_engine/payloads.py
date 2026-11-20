"""Rooms and messages as the client sees them. Account ids stay on the server:
members are known to each other by handle."""

from typing import Optional


def member_payload(row: dict) -> dict:
    return {"handle": row["handle"], "username": row["username"]}


def message_payload(row: dict) -> dict:
    sender = (
        {"handle": row["sender_handle"], "username": row["sender_username"]}
        if row["sender_handle"]
        else None
    )
    return {
        "id": row["id"],
        "body": row["body"],
        "attachment": row["attachment"],
        "created_at": row["created_at"].isoformat(),
        "mine": bool(row["mine"]),
        "sender": sender,
    }


def room_payload(row: dict) -> dict:
    last: Optional[dict] = None
    if row["last_id"] is not None:
        last = {
            "id": row["last_id"],
            "body": row["last_body"],
            "attachment": row["last_attachment"],
            "created_at": row["last_created_at"].isoformat(),
            "mine": bool(row["last_mine"]),
            "sender": (
                {"handle": row["last_handle"], "username": row["last_username"]}
                if row["last_handle"]
                else None
            ),
        }
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "members": [member_payload(m) for m in row["members"]],
        "unread": row["unread"],
        "last_message": last,
        "last_message_at": row["last_message_at"].isoformat(),
    }
