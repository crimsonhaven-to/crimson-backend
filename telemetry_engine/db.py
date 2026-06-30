"""Data layer for anonymous resolve telemetry.

One row per (source, day, env) with running ok/fail counts — deliberately
aggregate-only, so nothing here can identify a user or a title. The client batches
a watch session's per-source outcomes into a single beacon; `record_batch` folds
them into the daily counters with an upsert.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Tuple

from core.db_pool import get_connection, lock_schema_init

# Guard rails so a hostile/buggy client can't bloat the table or counters.
MAX_EVENTS_PER_BATCH = 60
MAX_SOURCE_LEN = 80
# "report" tags a manual "this source is broken" beacon from the player (vs the
# automatic per-source resolve outcomes), so the dashboard can tell them apart.
_VALID_ENVS = ("client", "extension", "proxied", "direct", "backend", "report")


def _today() -> date:
    return datetime.now(timezone.utc).date()


class TelemetryStore:
    """Daily per-source resolve success/failure aggregates."""

    def init_db(self) -> None:
        """Create the schema (idempotent)."""
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

    # --------------------------------------------------------------- ingest
    @staticmethod
    def _coalesce(events: Iterable[dict]) -> Dict[Tuple[str, str], List[int]]:
        """Fold a raw event list into {(source, env): [ok, fail]} totals, applying
        the validation/caps. Unknown shapes are skipped, not errored."""
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
            key = (source, env)
            slot = out.setdefault(key, [0, 0])
            if ev.get("ok"):
                slot[0] += 1
            else:
                slot[1] += 1
        return out

    def record_batch(self, events: Iterable[dict]) -> int:
        """Upsert a batch of {source, ok, env?} events into today's counters.
        Returns the number of distinct (source, env) rows touched."""
        folded = self._coalesce(events)
        if not folded:
            return 0
        today = _today()
        with get_connection() as conn:
            for (source, env), (ok, fail) in folded.items():
                conn.execute(
                    """
                    INSERT INTO resolve_telemetry (source, day, env, ok_count, fail_count)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (source, day, env) DO UPDATE
                        SET ok_count   = resolve_telemetry.ok_count   + EXCLUDED.ok_count,
                            fail_count = resolve_telemetry.fail_count + EXCLUDED.fail_count
                    """,
                    (source, today, env, ok, fail),
                )
        return len(folded)

    # ---------------------------------------------------------------- query
    def top_stats(self, days: int = 14) -> List[dict]:
        """Per-source aggregate over the last ``days`` days, busiest first.

        Each row: {source, ok, fail, total, success_rate (0-1), last_day}."""
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
            ok = int(r[1] or 0)
            fail = int(r[2] or 0)
            total = ok + fail
            out.append({
                "source": r[0],
                "ok": ok,
                "fail": fail,
                "total": total,
                "success_rate": round(ok / total, 4) if total else None,
                "last_day": r[3].isoformat() if r[3] else None,
            })
        return out

    def purge_old(self, keep_days: int = 120) -> int:
        """Delete rows older than ``keep_days`` (housekeeping). Returns row count."""
        cutoff = _today() - timedelta(days=keep_days)
        with get_connection() as conn:
            cur = conn.execute("DELETE FROM resolve_telemetry WHERE day < %s", (cutoff,))
            return cur.rowcount or 0
