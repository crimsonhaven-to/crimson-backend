"""The Ko-fi webhook rejects anything it cannot verify, with a 4xx and not a 500."""

import json
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from supporters_engine import routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KOFI_VERIFICATION_TOKEN", "tok")
    monkeypatch.setattr(routes.service, "record_payment", lambda event: True)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _post(client, data):
    return client.post(
        "/kofi/webhook",
        content=urlencode({"data": data}),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def test_a_verified_event_is_recorded(client):
    response = _post(client, json.dumps({"verification_token": "tok"}))
    assert response.status_code == 200
    assert response.json() == {"success": True, "recorded": True}


def test_a_wrong_token_is_refused(client):
    assert _post(client, json.dumps({"verification_token": "nope"})).status_code == 401


@pytest.mark.parametrize("data", ["not json", "[1, 2]", "42", "null"])
def test_malformed_data_is_a_400(client, data):
    assert _post(client, data).status_code == 400


def test_unconfigured_fails_closed(client, monkeypatch):
    monkeypatch.delenv("KOFI_VERIFICATION_TOKEN")
    assert _post(client, json.dumps({"verification_token": ""})).status_code == 503
