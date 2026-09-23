"""Data layer for anonymous resolve telemetry.

One row per (source, day, env) with running ok/fail counts. Aggregate only on
purpose, so nothing here can identify a user or a title. The client sends a watch
session's per-source outcomes as one beacon.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Iterable, List, Tuple

from core.clock import utc_now
from core.db_pool import get_connection, lock_schema_init

# So a hostile or buggy client cannot bloat the table or the counters.
MAX_EVENTS_PER_BATCH = 60
MAX_SOURCE_LEN = 80
# "report" is the player's manual "this source is broken" button, so the
# dashboard can tell it from an automatic resolve outcome.
_VALID_ENVS = ("client", "extension", "proxied", "direct", "backend", "report")


def _today() -> date:
    return utc_now().date()


def _coalesce(events: Iterable[dict]) -> Dict[Tuple[str, str], List[int]]:
    """Fold raw events into {(source, env): [ok, fail]}. Unknown shapes are
    skipped, not errors."""
    out: Dict[Tuple[str, str], List[int]] = {}
    for ev in list(events)[:MAX_EVENTS_PER_BATCH]:
        if not isinstance(ev, dict):
            continue
        source = (ev.get("source") or "").strip()[:MAX_SOURCE_LEN]
        if not source:
            continue
        env = (ev.get("env") or "client").strip().lower()
        if env not in _VALID_ENVS:
            env = "client"
        slot = out.setdefault((source, env), [0, 0])
        if ev.get("ok"):
            slot[0] += 1
        else:
            slot[1] += 1
    return out


class TelemetryStore:
    def init_db(self) -> None:
        with get_connection() as conn:
            lock_schema_init(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resolve_telemetry (
                    source     TEXT NOT NULL,
                    day        DATE NOT NULL,
                    env        TEXT NOT NULL DEFAULT 'client',
                    ok_count   BIGINT NOT NULL DEFAULT 0,
                    fail_count BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (source, day, env)
                );
                """
            )

    def record_batch(self, events: Iterable[dict]) -> int:
        """Add {source, ok, env?} events to today's counters. Returns the number
        of (source, env) rows touched."""
        folded = _coalesce(events)
        if not folded:
            return 0
        today = _today()
        with get_connection() as conn:
            conn.cursor().executemany(
                """
                INSERT INTO resolve_telemetry (source, day, env, ok_count, fail_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source, day, env) DO UPDATE
                    SET ok_count   = resolve_telemetry.ok_count   + EXCLUDED.ok_count,
                        fail_count = resolve_telemetry.fail_count + EXCLUDED.fail_count
                """,
                [(source, today, env, ok, fail) for (source, env), (ok, fail) in folded.items()],
            )
        return len(folded)

    def top_stats(self, days: int = 14) -> List[dict]:
        """Per-source totals over the last ``days`` days, busiest first."""
        days = max(1, min(days, 365))
        since = _today() - timedelta(days=days - 1)
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT source,
                       SUM(ok_count)   AS ok,
                       SUM(fail_count) AS fail,
                       MAX(day)        AS last_day
                FROM resolve_telemetry
                WHERE day >= %s
                GROUP BY source
                ORDER BY (SUM(ok_count) + SUM(fail_count)) DESC, source ASC
                """,
                (since,),
            ).fetchall()

        out: List[dict] = []
        for r in rows:
            ok = int(r["ok"] or 0)
            fail = int(r["fail"] or 0)
            total = ok + fail
            out.append({
                "source": r["source"],
                "ok": ok,
                "fail": fail,
                "total": total,
                "success_rate": round(ok / total, 4) if total else None,
                "last_day": r["last_day"].isoformat() if r["last_day"] else None,
            })
        return out

    def purge_old(self, keep_days: int = 120) -> int:
        """Returns the number of rows deleted."""
        cutoff = _today() - timedelta(days=keep_days)
        with get_connection() as conn:
            cur = conn.execute("DELETE FROM resolve_telemetry WHERE day < %s", (cutoff,))
            return cur.rowcount or 0


store = TelemetryStore()
