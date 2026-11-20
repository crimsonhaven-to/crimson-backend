"""Picking the unique @handle other members message you by."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from account_engine import profile_routes
from account_engine.deps import require_user
from core.rate_limit import limiter


@pytest.fixture
def saved(monkeypatch):
    taken = {"ami"}
    calls = []

    def set_handle(user_id, handle):
        calls.append(handle)
        return handle not in taken

    monkeypatch.setattr(profile_routes.store, "set_handle", set_handle)
    return calls


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(profile_routes.router)
    app.dependency_overrides[require_user] = lambda: {"user_id": 1, "handle": "ray"}
    return TestClient(app)


def test_a_handle_is_stored_lowercase_without_the_at(client, saved):
    res = client.put("/account/handle", json={"handle": " @Raymond_1 "})
    assert res.json()["handle"] == "raymond_1"
    assert saved == ["raymond_1"]


@pytest.mark.parametrize("handle", ["ab", "1ray", "ray.mond", "a" * 21, "ray mond"])
def test_a_malformed_handle_is_refused(client, saved, handle):
    assert client.put("/account/handle", json={"handle": handle}).status_code == 400
    assert saved == []


def test_a_taken_handle_is_409(client, saved):
    assert client.put("/account/handle", json={"handle": "ami"}).status_code == 409


def test_keeping_your_own_handle_is_not_a_conflict(client, saved):
    assert client.put("/account/handle", json={"handle": "RAY"}).status_code == 200
    assert saved == []
