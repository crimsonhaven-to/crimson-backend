from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from account_engine.deps import require_user
from core.rate_limit import limiter
from messaging_engine import block_routes, message_routes, room_routes
from messaging_engine.db import store

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
RAY = {"user_id": 1, "handle": "ray", "username": "Raymond"}
AMI = {"user_id": 2, "handle": "ami", "username": None}
BO = {"user_id": 3, "handle": "bo", "username": "Bo"}
ACCOUNTS = [RAY, AMI, BO]


def room_row(room_id=10, kind="direct", members=(RAY, AMI), name=None):
    return {
        "id": room_id, "kind": kind, "name": name, "last_message_at": NOW, "unread": 0,
        "last_id": None, "last_body": None, "last_attachment": None, "last_created_at": None,
        "last_mine": None, "last_handle": None, "last_username": None,
        "members": list(members),
    }


@pytest.fixture
def caller():
    """Who is signed in; a test may swap it before making requests."""
    return dict(RAY)


@pytest.fixture
def client(caller, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(
        store, "members_by_handles",
        lambda handles: [a for a in ACCOUNTS if a["handle"] in handles],
    )
    monkeypatch.setattr(store, "has_blocked", lambda user_id, other_id: False)
    monkeypatch.setattr(store, "blocked_by_any", lambda user_id, others: [])
    app = FastAPI()
    app.state.limiter = limiter
    for module in (room_routes, message_routes, block_routes):
        app.include_router(module.router)
    app.dependency_overrides[require_user] = lambda: caller
    return TestClient(app)
