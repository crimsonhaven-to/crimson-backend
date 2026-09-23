"""Session storage. A session's ``token_hash`` must never leave the store under any
key: it is the SHA-256 of a live bearer token, so publishing it publishes the
lookup key for the session table. The connection is faked, so no database.
"""

from datetime import datetime, timedelta, timezone

import pytest





from account_engine import db as account_db

from account_engine.db import _hash_token, _public_session_id


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


@pytest.fixture
def use_conn(monkeypatch):
    """Point the account store at a fake connection and return the store."""
    def use(conn):
        monkeypatch.setattr(account_db, "get_connection", lambda: conn)
        return account_db.store
    return use


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


def test_public_session_id_is_not_the_token_hash():
    token_hash = _hash_token("tok-a")
    public = _public_session_id(token_hash)
    assert public != token_hash
    assert public not in token_hash and token_hash not in public


def test_public_session_id_is_stable():
    assert _public_session_id("abc") == _public_session_id("abc")
    assert _public_session_id("abc") != _public_session_id("abd")


def test_list_sessions_never_returns_the_hash_under_any_key(use_conn):
    token_hash = _hash_token("tok-a")
    store = use_conn(FakeConn({"FROM sessions WHERE user_id": [_session_row("tok-a")]}))
    sessions = store.list_sessions(7, current_token="tok-a")
    assert len(sessions) == 1
    # Not "no key called token_hash": no value equal to it anywhere.
    assert token_hash not in str(sessions)
    assert "token_hash" not in sessions[0]


def test_list_sessions_flags_the_caller(use_conn):
    store = use_conn(FakeConn({
        "FROM sessions WHERE user_id": [_session_row("mine"), _session_row("theirs")],
    }))
    sessions = store.list_sessions(7, current_token="mine")
    assert [s["current"] for s in sessions] == [True, False]


def test_list_sessions_without_a_token_flags_nothing(use_conn):
    store = use_conn(FakeConn({"FROM sessions WHERE user_id": [_session_row("mine")]}))
    assert store.list_sessions(7, current_token=None)[0]["current"] is False


def test_a_session_with_no_device_columns_still_lists(use_conn):
    """Rows predating migration 005 carry NULLs. Hiding them would hide exactly
    the session a user most needs to see."""
    store = use_conn(FakeConn({
        "FROM sessions WHERE user_id": [
            _session_row("old", user_agent=None, ip=None, last_seen_at=None)
        ],
    }))
    session = store.list_sessions(7)[0]
    assert session["user_agent"] is None and session["ip"] is None
    assert session["id"]


def test_revoke_session_matches_by_public_id(use_conn):
    row = _session_row("tok-a")
    conn = FakeConn({"SELECT token_hash FROM sessions WHERE user_id": [row]})
    store = use_conn(conn)
    assert store.revoke_session(7, _public_session_id(row["token_hash"])) is True
    deletes = [sql for sql, _ in conn.executed if sql.startswith("DELETE")]
    assert len(deletes) == 1


def test_revoke_session_rejects_an_unknown_id(use_conn):
    conn = FakeConn({"SELECT token_hash FROM sessions WHERE user_id": [_session_row("tok-a")]})
    store = use_conn(conn)
    assert store.revoke_session(7, "0" * 32) is False
    assert not [sql for sql, _ in conn.executed if sql.startswith("DELETE")]


def test_revoke_session_only_ever_looks_at_one_user(use_conn):
    """The public id is derived from a value an attacker cannot compute, but the
    query is still scoped to the caller so a guessed id can never reach another
    account's row."""
    conn = FakeConn({"SELECT token_hash FROM sessions WHERE user_id": []})
    store = use_conn(conn)
    store.revoke_session(7, "whatever")
    sql, params = conn.executed[0]
    assert "WHERE user_id = %s" in sql and params == (7,)


def test_revoke_others_keeps_the_caller(use_conn):
    conn = FakeConn({"DELETE FROM sessions WHERE user_id": 3})
    store = use_conn(conn)
    assert store.revoke_other_sessions(7, "mine") == 3
    sql, params = conn.executed[0]
    assert "token_hash <> %s" in sql
    assert params == (7, _hash_token("mine"))


def test_revoke_others_without_a_token_drops_everything(use_conn):
    conn = FakeConn({"DELETE FROM sessions WHERE user_id": 4})
    store = use_conn(conn)
    assert store.revoke_other_sessions(7, None) == 4
    assert "token_hash" not in conn.executed[0][0]


def test_validate_and_touch_session_is_one_statement(use_conn):
    """The login wall keeps a 60s validity cache precisely so a request does not
    hit the database. Stamping last_seen_at in a second statement would halve
    that saving; doing it in the check itself costs nothing."""
    conn = FakeConn({"UPDATE sessions SET last_seen_at": 1})
    store = use_conn(conn)
    assert store.validate_and_touch_session("tok-a") is True
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "UPDATE sessions SET last_seen_at" in sql and "expires_at > %s" in sql
    assert params[1] == _hash_token("tok-a")


def test_validate_and_touch_session_rejects_an_empty_token(use_conn):
    store = use_conn(None)
    assert store.validate_and_touch_session("") is False


def test_expired_session_does_not_validate(use_conn):
    conn = FakeConn({"UPDATE sessions SET last_seen_at": 0})
    store = use_conn(conn)
    assert store.validate_and_touch_session("tok-a") is False
