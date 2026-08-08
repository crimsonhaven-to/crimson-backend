"""
Chat storage: operator settings, conversations, and the usage ledger.

No ``init_db()`` here on purpose. The schema lives entirely in
``migrations/002_lumi_chat.sql`` per the convention 000_baseline.sql sets out, so
this module only reads and writes tables the migration runner has already made.

Everything is synchronous psycopg against the shared pool, matching the other
stores. Callers are async, so they wrap these in ``run_in_threadpool``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from core.db_pool import get_connection

from .models import DEFAULT_MODEL, PROVIDERS, resolve

logger = logging.getLogger("crimson.chat.db")

# Threads untouched for this long are pruned. Long enough that "carry on from
# yesterday" works, short enough that the table stays small and old watch habits
# do not linger indefinitely.
CONVERSATION_TTL_DAYS = 30

# The ledger is what the dashboard charts, so it outlives the conversations it
# describes. Still bounded, because it is per provider call and grows fastest.
USAGE_TTL_DAYS = 180


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _month_start() -> str:
    now = _now()
    return _iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


class ChatStore:
    def _connect(self):
        return get_connection()

    # -- settings -----------------------------------------------------------
    def get_settings(self) -> Dict:
        """The single operator settings row, normalised.

        Falls back to a sane default set if the row is somehow missing, so a
        half-applied migration degrades to "feature off" rather than a 500 on
        every request.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chat_settings WHERE id = 1").fetchone()
        if not row:
            return {
                "enabled": False,
                "provider": "anthropic",
                "model": DEFAULT_MODEL["anthropic"],
                "monthly_token_budget": 2_000_000,
                "history_turns": 12,
                "max_tool_iterations": 5,
            }
        out = dict(row)
        out["enabled"] = bool(out.get("enabled"))
        provider = out.get("provider") if out.get("provider") in PROVIDERS else "anthropic"
        out["provider"] = provider
        # Guards a provider switch that left the other vendor's model id stored.
        out["model"] = resolve(provider, out.get("model")).model_id
        return out

    def update_settings(self, patch: Dict, *, updated_by: Optional[int] = None) -> Dict:
        """Apply a partial settings update. Unknown keys are ignored."""
        allowed = (
            "enabled",
            "provider",
            "model",
            "monthly_token_budget",
            "history_turns",
            "max_tool_iterations",
        )
        sets = []
        params: List = []
        for key in allowed:
            if key in patch and patch[key] is not None:
                sets.append(f"{key} = %s")
                params.append(patch[key])
        if not sets:
            return self.get_settings()

        sets.append("updated_at = %s")
        params.append(_iso(_now()))
        sets.append("updated_by = %s")
        params.append(updated_by)

        with self._connect() as conn:
            conn.execute(
                f"UPDATE chat_settings SET {', '.join(sets)} WHERE id = 1", tuple(params)
            )
        return self.get_settings()

    # -- per-account access -------------------------------------------------
    def set_chat_access(self, user_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET chat_enabled = %s WHERE user_id = %s",
                (enabled, user_id),
            )

    def set_user_budget(self, user_id: int, budget: Optional[int]) -> None:
        """Per-user monthly token ceiling. None restores the global default."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET chat_monthly_token_budget = %s WHERE user_id = %s",
                (budget, user_id),
            )

    # -- conversations ------------------------------------------------------
    def get_or_create_conversation(
        self, user_id: int, conversation_id: Optional[int]
    ) -> int:
        """Resolve a conversation id, creating one when absent.

        A conversation id from another account resolves to a NEW conversation
        rather than raising, so a stale id in a browser tab cannot be used to
        probe for, or read, someone else's thread.
        """
        now = _iso(_now())
        with self._connect() as conn:
            if conversation_id:
                row = conn.execute(
                    "SELECT conversation_id FROM chat_conversations"
                    " WHERE conversation_id = %s AND user_id = %s",
                    (conversation_id, user_id),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE chat_conversations SET updated_at = %s WHERE conversation_id = %s",
                        (now, conversation_id),
                    )
                    return int(row["conversation_id"])
            row = conn.execute(
                "INSERT INTO chat_conversations (user_id, created_at, updated_at)"
                " VALUES (%s, %s, %s) RETURNING conversation_id",
                (user_id, now, now),
            ).fetchone()
            return int(row["conversation_id"])

    def add_message(
        self,
        conversation_id: int,
        user_id: int,
        role: str,
        content: str,
        actions: Optional[List[Dict]] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_messages"
                " (conversation_id, user_id, role, content, actions, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    conversation_id,
                    user_id,
                    role,
                    content,
                    json.dumps(actions) if actions else None,
                    _iso(_now()),
                ),
            )

    def history(self, conversation_id: int, user_id: int, turns: int) -> List[Dict]:
        """The last ``turns`` exchanges, oldest first.

        ``turns`` counts exchanges, not rows, so the limit is doubled. This is the
        main control on how input cost grows over a long conversation.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, actions FROM chat_messages"
                " WHERE conversation_id = %s AND user_id = %s"
                " ORDER BY message_id DESC LIMIT %s",
                (conversation_id, user_id, max(2, turns * 2)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def list_conversations(self, user_id: int, limit: int = 20) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT conversation_id, title, created_at, updated_at"
                " FROM chat_conversations WHERE user_id = %s"
                " ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_title(self, conversation_id: int, user_id: int, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_conversations SET title = %s"
                " WHERE conversation_id = %s AND user_id = %s AND title IS NULL",
                (title[:120], conversation_id, user_id),
            )

    def delete_conversation(self, conversation_id: int, user_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM chat_conversations WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, user_id),
            )
            return cur.rowcount > 0

    # -- usage ledger -------------------------------------------------------
    def record_usage(
        self,
        user_id: int,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        cost_micros: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_usage"
                " (user_id, provider, model, input_tokens, output_tokens,"
                "  cached_tokens, cost_micros, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    user_id,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    cost_micros,
                    _iso(_now()),
                ),
            )

    def tokens_this_month(self, user_id: int) -> int:
        """Total billable tokens this calendar month, for budget enforcement.

        Cached reads are counted. They cost a tenth as much but they are still
        consumption, and a budget that ignored them would drift from the cost
        chart sitting next to it in the dashboard.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens + cached_tokens), 0) AS n"
                " FROM chat_usage WHERE user_id = %s AND created_at >= %s",
                (user_id, _month_start()),
            ).fetchone()
        return int(row["n"] or 0)

    def effective_budget(self, user: Dict, settings: Dict) -> int:
        """This account's monthly ceiling: its own if set, else the global one."""
        own = user.get("chat_monthly_token_budget")
        if own is not None:
            return int(own)
        return int(settings.get("monthly_token_budget") or 0)

    def usage_overview(self, days: int = 30) -> Dict:
        """Aggregates for the admin Lumi tab."""
        since = _iso(_now() - timedelta(days=days))
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                "       COALESCE(SUM(cached_tokens), 0) AS cached_tokens,"
                "       COALESCE(SUM(cost_micros), 0)  AS cost_micros,"
                "       COUNT(*) AS calls"
                " FROM chat_usage WHERE created_at >= %s",
                (since,),
            ).fetchone()
            per_user = conn.execute(
                "SELECT u.user_id, a.email, a.username,"
                "       SUM(u.input_tokens + u.output_tokens + u.cached_tokens) AS tokens,"
                "       SUM(u.cost_micros) AS cost_micros, COUNT(*) AS calls"
                " FROM chat_usage u JOIN accounts a ON a.user_id = u.user_id"
                " WHERE u.created_at >= %s"
                " GROUP BY u.user_id, a.email, a.username"
                " ORDER BY cost_micros DESC LIMIT 25",
                (since,),
            ).fetchall()
            month = conn.execute(
                "SELECT COALESCE(SUM(cost_micros), 0) AS cost_micros"
                " FROM chat_usage WHERE created_at >= %s",
                (_month_start(),),
            ).fetchone()
            granted = conn.execute(
                "SELECT COUNT(*) AS n FROM accounts WHERE chat_enabled = TRUE"
            ).fetchone()
            convos = conn.execute(
                "SELECT COUNT(*) AS n FROM chat_conversations"
            ).fetchone()

        return {
            "window_days": days,
            "totals": dict(totals),
            "per_user": [dict(r) for r in per_user],
            "month_to_date_cost_micros": int(month["cost_micros"] or 0),
            "users_granted": int(granted["n"] or 0),
            "conversations": int(convos["n"] or 0),
        }

    # -- maintenance --------------------------------------------------------
    def prune(self) -> Dict:
        """Drop stale conversations and ancient ledger rows.

        Registered on the same scheduler as the other nightly maintenance, on the
        RUN_DB_SYNC replica only, so it runs once per cluster rather than once
        per container. Messages go with their conversation via ON DELETE CASCADE.
        """
        conv_cutoff = _iso(_now() - timedelta(days=CONVERSATION_TTL_DAYS))
        usage_cutoff = _iso(_now() - timedelta(days=USAGE_TTL_DAYS))
        with self._connect() as conn:
            conversations = conn.execute(
                "DELETE FROM chat_conversations WHERE updated_at < %s", (conv_cutoff,)
            ).rowcount
            usage = conn.execute(
                "DELETE FROM chat_usage WHERE created_at < %s", (usage_cutoff,)
            ).rowcount
        if conversations or usage:
            logger.info(
                "chat prune: removed %s conversations, %s usage rows", conversations, usage
            )
        return {"conversations": conversations, "usage": usage}
