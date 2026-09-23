"""The security event ledger. The owner's own view is a whitelist with no
``detail`` and no ``identity``, filtered on user_id alone.
"""

from datetime import datetime, timezone



from account_engine import audit





from datetime import date



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


def test_zero_filled_series_covers_every_day_in_window():
    today = date(2026, 7, 10)
    series = audit.zero_filled_series([], days=7, today=today)
    assert len(series) == 7
    assert series[0]["day"] == "2026-07-04"
    assert series[-1]["day"] == "2026-07-10"
    assert all(s["total"] == 0 and s["failures"] == 0 and s["by_type"] == {} for s in series)


def test_zero_filled_series_folds_types_and_failures():
    today = date(2026, 7, 10)
    rows = [
        {"day": date(2026, 7, 9), "event_type": "login_failed", "outcome": "failure", "n": 3},
        {"day": date(2026, 7, 9), "event_type": "login_success", "outcome": "success", "n": 2},
        {"day": date(2026, 7, 10), "event_type": "invite_invalid", "outcome": "failure", "n": 1},
        # Outside the window -> silently dropped, not a crash.
        {"day": date(2026, 6, 1), "event_type": "login_failed", "outcome": "failure", "n": 9},
    ]
    series = audit.zero_filled_series(rows, days=3, today=today)
    by_day = {s["day"]: s for s in series}
    assert by_day["2026-07-09"]["total"] == 5
    assert by_day["2026-07-09"]["failures"] == 3
    assert by_day["2026-07-09"]["by_type"] == {"login_failed": 3, "login_success": 2}
    assert by_day["2026-07-10"]["by_type"] == {"invite_invalid": 1}
    assert by_day["2026-07-08"]["total"] == 0


def test_fold_top_ips_ranks_and_breaks_down_by_type():
    seen = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)
    rows = [
        {"ip": "1.2.3.4", "event_type": "login_failed", "n": 5, "last_seen": seen},
        {"ip": "1.2.3.4", "event_type": "invite_invalid", "n": 2, "last_seen": later},
        {"ip": "5.6.7.8", "event_type": "login_failed", "n": 3, "last_seen": seen},
        {"ip": None, "event_type": "login_failed", "n": 99, "last_seen": seen},  # no IP -> skipped
    ]
    top = audit.fold_top_ips(rows)
    assert [t["ip"] for t in top] == ["1.2.3.4", "5.6.7.8"]
    assert top[0]["count"] == 7
    assert top[0]["types"] == {"login_failed": 5, "invite_invalid": 2}
    assert top[0]["last_seen"] == later.isoformat()  # newest of the folded rows wins


def test_fold_top_ips_respects_limit():
    rows = [
        {"ip": f"10.0.0.{i}", "event_type": "login_failed", "n": i, "last_seen": None}
        for i in range(1, 30)
    ]
    top = audit.fold_top_ips(rows, limit=10)
    assert len(top) == 10
    assert top[0]["ip"] == "10.0.0.29"  # highest count first


def test_encode_detail_drops_oversized_and_unserializable():
    assert audit.encode_detail(None) is None
    assert audit.encode_detail({}) is None
    assert audit.encode_detail({"reason": "bad_credentials"}) == '{"reason": "bad_credentials"}'
    huge = {"blob": "x" * (audit.MAX_DETAIL_LEN + 10)}
    assert audit.encode_detail(huge) is None  # detail dropped, event survives


def test_key_prefix_never_leaks_the_full_key():
    pk = "ab" * 32  # 64 hex chars
    prefix = audit.key_prefix(pk)
    assert prefix == "abababababab…"
    assert len(prefix) < 20
    assert audit.key_prefix(None) is None
    assert audit.key_prefix("") is None


def test_fold_type_totals_sums_days_per_type_and_outcome():
    rows = [
        {"day": date(2026, 7, 9), "event_type": "login_failed", "outcome": "failure", "n": 3},
        {"day": date(2026, 7, 10), "event_type": "login_failed", "outcome": "failure", "n": 4},
        {"day": date(2026, 7, 10), "event_type": "login_success", "outcome": "success", "n": 5},
    ]
    assert audit.fold_type_totals(rows) == [
        {"event_type": "login_failed", "outcome": "failure", "count": 7},
        {"event_type": "login_success", "outcome": "success", "count": 5},
    ]
