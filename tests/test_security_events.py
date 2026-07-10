"""Security event ledger (account_engine/audit.py).

The INSERT/aggregate SQL needs Postgres, but the folding that shapes the
dashboard payloads (zero-filled chart series, top-offender ranking) and the
defensive field handling are pure and worth pinning.
"""

from datetime import date, datetime, timezone

from account_engine import audit


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
