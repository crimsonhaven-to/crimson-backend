"""
The user-facing security surface.

Two properties carry real weight and both are tested by construction rather than
by inspection of a happy path:

  * a session's ``token_hash`` never leaves account_engine.db, under any key
    name, on any path. It is the SHA-256 of a live bearer token, so publishing
    it publishes the lookup key for the session table.
  * the account's own view of the security ledger is a whitelist, carries no
    ``detail`` and no ``identity``, and is filtered on user_id alone.

No database: the store's connection is faked, which is the same seam the airing
tests use.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from account_engine import audit, auth, passwords, security_routes
from account_engine.deps import parse_bearer
from account_engine.schemas import DeleteAccountRequest
from account_engine.db import AccountStore, _hash_token, _public_session_id


# --- fakes ------------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows, rowcount=None):
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Answers by matching a fragment of the SQL, so a query can be re-worded
    without rewriting the test, but a query that changes shape cannot pass."""

    def __init__(self, answers):
        self.answers = answers
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        for fragment, rows in self.answers.items():
            if fragment in " ".join(sql.split()):
                return FakeCursor(rows if isinstance(rows, list) else [], rowcount=
                                  len(rows) if isinstance(rows, list) else rows)
        return FakeCursor([])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _store(answers):
    store = AccountStore()
    store._connect = lambda: FakeConn(answers)
    return store


def _session_row(token="tok-a", **overrides):
    now = datetime.now(timezone.utc)
    row = {
        "token_hash": _hash_token(token),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
        "user_agent": "Mozilla/5.0 (Windows NT 10.0)",
        "ip": "203.0.113.7",
        "last_seen_at": now.isoformat(),
    }
    row.update(overrides)
    return row


# --- the token hash never leaves --------------------------------------------

def test_public_session_id_is_not_the_token_hash():
    token_hash = _hash_token("tok-a")
    public = _public_session_id(token_hash)
    assert public != token_hash
    assert public not in token_hash and token_hash not in public


def test_public_session_id_is_stable():
    assert _public_session_id("abc") == _public_session_id("abc")
    assert _public_session_id("abc") != _public_session_id("abd")


def test_list_sessions_never_returns_the_hash_under_any_key():
    token_hash = _hash_token("tok-a")
    store = _store({"FROM sessions WHERE user_id": [_session_row("tok-a")]})
    sessions = store.list_sessions(7, current_token="tok-a")
    assert len(sessions) == 1
    # Not "no key called token_hash": no value equal to it anywhere.
    assert token_hash not in str(sessions)
    assert "token_hash" not in sessions[0]


def test_list_sessions_flags_the_caller():
    store = _store({
        "FROM sessions WHERE user_id": [_session_row("mine"), _session_row("theirs")],
    })
    sessions = store.list_sessions(7, current_token="mine")
    assert [s["current"] for s in sessions] == [True, False]


def test_list_sessions_without_a_token_flags_nothing():
    store = _store({"FROM sessions WHERE user_id": [_session_row("mine")]})
    assert store.list_sessions(7, current_token=None)[0]["current"] is False


def test_a_session_with_no_device_columns_still_lists():
    """Rows predating migration 005 carry NULLs. Hiding them would hide exactly
    the session a user most needs to see."""
    store = _store({
        "FROM sessions WHERE user_id": [
            _session_row("old", user_agent=None, ip=None, last_seen_at=None)
        ],
    })
    session = store.list_sessions(7)[0]
    assert session["user_agent"] is None and session["ip"] is None
    assert session["id"]


# --- revocation --------------------------------------------------------------

def test_revoke_session_matches_by_public_id():
    row = _session_row("tok-a")
    conn = FakeConn({"SELECT token_hash FROM sessions WHERE user_id": [row]})
    store = AccountStore()
    store._connect = lambda: conn
    assert store.revoke_session(7, _public_session_id(row["token_hash"])) is True
    deletes = [sql for sql, _ in conn.executed if sql.startswith("DELETE")]
    assert len(deletes) == 1


def test_revoke_session_rejects_an_unknown_id():
    conn = FakeConn({"SELECT token_hash FROM sessions WHERE user_id": [_session_row("tok-a")]})
    store = AccountStore()
    store._connect = lambda: conn
    assert store.revoke_session(7, "0" * 32) is False
    assert not [sql for sql, _ in conn.executed if sql.startswith("DELETE")]


def test_revoke_session_only_ever_looks_at_one_user():
    """The public id is derived from a value an attacker cannot compute, but the
    query is still scoped to the caller so a guessed id can never reach another
    account's row."""
    conn = FakeConn({"SELECT token_hash FROM sessions WHERE user_id": []})
    store = AccountStore()
    store._connect = lambda: conn
    store.revoke_session(7, "whatever")
    sql, params = conn.executed[0]
    assert "WHERE user_id = %s" in sql and params == (7,)


def test_revoke_others_keeps_the_caller():
    conn = FakeConn({"DELETE FROM sessions WHERE user_id": 3})
    store = AccountStore()
    store._connect = lambda: conn
    assert store.revoke_other_sessions(7, "mine") == 3
    sql, params = conn.executed[0]
    assert "token_hash <> %s" in sql
    assert params == (7, _hash_token("mine"))


def test_revoke_others_without_a_token_drops_everything():
    conn = FakeConn({"DELETE FROM sessions WHERE user_id": 4})
    store = AccountStore()
    store._connect = lambda: conn
    assert store.revoke_other_sessions(7, None) == 4
    assert "token_hash" not in conn.executed[0][0]


# --- last_seen_at costs one write per TTL, not per request -------------------

def test_validate_and_touch_session_is_one_statement():
    """The login wall keeps a 60s validity cache precisely so a request does not
    hit the database. Stamping last_seen_at in a second statement would halve
    that saving; doing it in the check itself costs nothing."""
    conn = FakeConn({"UPDATE sessions SET last_seen_at": 1})
    store = AccountStore()
    store._connect = lambda: conn
    assert store.validate_and_touch_session("tok-a") is True
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "UPDATE sessions SET last_seen_at" in sql and "expires_at > %s" in sql
    assert params[1] == _hash_token("tok-a")


def test_validate_and_touch_session_rejects_an_empty_token():
    store = AccountStore()
    store._connect = lambda: pytest.fail("must not reach the database")
    assert store.validate_and_touch_session("") is False


def test_expired_session_does_not_validate():
    conn = FakeConn({"UPDATE sessions SET last_seen_at": 0})
    store = AccountStore()
    store._connect = lambda: conn
    assert store.validate_and_touch_session("tok-a") is False


# --- the ledger, as its owner sees it ----------------------------------------

def test_owner_view_drops_detail_and_identity():
    row = {
        "ts": datetime(2026, 9, 12, tzinfo=timezone.utc),
        "event_type": "login_failed",
        "outcome": "failure",
        "ip": "203.0.113.7",
        "user_agent": "curl/8",
        "identity": "someone@example.com",
        "detail": '{"reason": "bad_credentials"}',
    }
    public = audit._row_for_owner(row)
    assert set(public) == {"ts", "event_type", "outcome", "ip", "user_agent"}
    assert "someone@example.com" not in str(public)
    assert "bad_credentials" not in str(public)


def test_admin_actions_are_not_user_visible():
    """admin_action describes what an operator did, often to someone else."""
    assert "admin_action" not in audit.USER_VISIBLE_EVENTS


def test_user_visible_events_are_all_real_event_types():
    """A typo here silently hides a category forever, since the filter is an
    equality match against this tuple."""
    import re
    from pathlib import Path

    emitted = set()
    for path in Path("account_engine").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        emitted.update(re.findall(r'log_event\(\s*"([a-z_]+)"', source))
        emitted.update(re.findall(r'log_event,\s*\n?\s*"([a-z_]+)"', source))
    unknown = [e for e in audit.USER_VISIBLE_EVENTS if e not in emitted]
    assert unknown == [], f"whitelisted but never emitted: {unknown}"


def test_events_query_filters_on_user_id_only():
    """Matching on identity as well would answer "does this address have an
    account" for any registered user, which is an enumeration oracle."""
    conn = FakeConn({"FROM security_events": []})
    audit.get_connection = lambda: conn
    try:
        audit.list_events_for_user(7, 50)
    finally:
        from core.db_pool import get_connection
        audit.get_connection = get_connection
    sql, params = conn.executed[0]
    assert "WHERE user_id = %s AND event_type = ANY(%s)" in sql
    assert "identity" not in sql
    assert params[0] == 7


# --- deletion confirmation ---------------------------------------------------

class _Req:
    headers: dict = {}
    client = None


def test_password_account_must_supply_the_right_password(monkeypatch):
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)
    user = {"user_id": 7, "password_hash": passwords.hash_password("correct horse")}
    body = DeleteAccountRequest(password="wrong horse")
    with pytest.raises(HTTPException) as excinfo:
        auth.confirm_owner(user, body, _Req())
    assert excinfo.value.status_code == 401


def test_password_account_passes_with_the_right_password():
    user = {"user_id": 7, "password_hash": passwords.hash_password("correct horse")}
    body = DeleteAccountRequest(password="correct horse")
    auth.confirm_owner(user, body, _Req())


def test_a_bearer_token_alone_never_confirms():
    """The whole point: the credential an attacker can steal is not enough."""
    user = {"user_id": 7, "password_hash": passwords.hash_password("pw")}
    with pytest.raises(HTTPException):
        auth.confirm_owner(user, DeleteAccountRequest(), _Req())


def test_mnemonic_account_needs_a_signature():
    user = {"user_id": 7, "password_hash": None, "public_key": "ab" * 32}
    with pytest.raises(HTTPException) as excinfo:
        auth.confirm_owner(user, DeleteAccountRequest(), _Req())
    assert excinfo.value.status_code == 400


def test_mnemonic_account_verifies_the_signature(monkeypatch):
    seen = {}

    def fake_verify(public_key, challenge, signature, request, flow):
        seen.update(public_key=public_key, challenge=challenge, flow=flow)

    monkeypatch.setattr(auth, "verify_signed_challenge", fake_verify)
    user = {"user_id": 7, "password_hash": None, "public_key": "ab" * 32}
    body = DeleteAccountRequest(challenge="c", signature="s")
    auth.confirm_owner(user, body, _Req())
    assert seen["public_key"] == "ab" * 32 and seen["flow"] == "delete_account"


def test_an_account_with_neither_credential_is_refused():
    user = {"user_id": 7, "password_hash": None, "public_key": None}
    with pytest.raises(HTTPException) as excinfo:
        auth.confirm_owner(user, DeleteAccountRequest(), _Req())
    assert excinfo.value.status_code == 400


# --- export ------------------------------------------------------------------

def test_export_omits_the_credential_and_the_admin_flag(monkeypatch):
    account = {
        "user_id": 7, "email": "a@b.c", "public_key": None,
        "password_hash": "pbkdf2_sha256$...", "is_admin": True, "label": "me",
    }
    monkeypatch.setattr(security_routes.store, "get_account", lambda uid: account)
    monkeypatch.setattr(security_routes.store, "get_preferences", lambda uid: {"theme": "dark"})
    monkeypatch.setattr(security_routes.store, "list_favorites", lambda uid: [{"item_key": "anilist:21"}])
    monkeypatch.setattr(security_routes.store, "list_progress", lambda uid: [{"item_key": "anilist:21:s1:e5"}])
    from notify_engine.db import store as airing_store
    monkeypatch.setattr(airing_store, "list_subscriptions", lambda uid: [])

    payload = security_routes._collect_export(7)
    assert "password_hash" not in payload["account"]
    assert "is_admin" not in payload["account"]
    assert payload["account"]["email"] == "a@b.c"
    assert payload["watchlists"] and payload["progress"]
    assert payload["preferences"] == {"theme": "dark"}


# --- bearer parsing ----------------------------------------------------------

@pytest.mark.parametrize("header,expected", [
    ("Bearer abc", "abc"),
    ("bearer abc", "abc"),
    ("BEARER  abc ", "abc"),
    ("Basic abc", None),
    ("abc", None),
    (None, None),
    ("", None),
])
def test_bearer_token_parsing(header, expected):
    assert parse_bearer(header) == expected
