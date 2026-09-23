"""
Chat storage: operator settings, conversations, and the usage ledger.

No ``init_db()`` on purpose. The schema lives in ``migrations/002_lumi_chat.sql``
per the convention 000_baseline.sql sets out, so this only reads and writes tables
the migration runner has already made.

Synchronous psycopg against the shared pool, like the other stores, so async
callers run these in a thread.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from core.clock import utc_now, utc_now_iso
from core.db_pool import get_connection

from .models import ANTHROPIC, DEFAULT_MODEL, PROVIDERS, resolve

logger = logging.getLogger("crimson.chat.db")

# Long enough that "carry on from yesterday" works, short enough that the table
# stays small and old watch habits do not linger.
CONVERSATION_TTL_DAYS = 30

# The ledger is what the dashboard charts, so it outlives the conversations it
# describes. Still bounded, since it grows fastest of anything here.
USAGE_TTL_DAYS = 180


def _month_start() -> str:
    now = utc_now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


class ChatStore:
    def get_settings(self) -> Dict:
        """The single operator settings row, normalised.

        Falls back to defaults if the row is missing, so a half-applied migration
        degrades to "feature off" rather than a 500 on every request.
        """
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM chat_settings WHERE id = 1").fetchone()
        if not row:
            return {
                "enabled": False,
                "provider": ANTHROPIC,
                "model": DEFAULT_MODEL[ANTHROPIC],
                "monthly_token_budget": 2_000_000,
                "history_turns": 12,
                "max_tool_iterations": 5,
            }
        out = dict(row)
        out["enabled"] = bool(out.get("enabled"))
        # Narrowed rather than trusted, so a hand-edited database degrades to the
        # default instead of sending an unknown provider down the request path.
        stored = out.get("provider")
        provider = stored if isinstance(stored, str) and stored in PROVIDERS else ANTHROPIC
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
        params.append(utc_now_iso())
        sets.append("updated_by = %s")
        params.append(updated_by)

        with get_connection() as conn:
            conn.execute(
                f"UPDATE chat_settings SET {', '.join(sets)} WHERE id = 1", tuple(params)
            )
        return self.get_settings()

    def set_chat_access(self, user_id: int, enabled: bool) -> None:
        with get_connection() as conn:
            conn.execute(
                "UPDATE accounts SET chat_enabled = %s WHERE user_id = %s",
                (enabled, user_id),
            )

    def set_user_budget(self, user_id: int, budget: Optional[int]) -> None:
        """Per-user monthly token ceiling; None restores the global default."""
        with get_connection() as conn:
            conn.execute(
                "UPDATE accounts SET chat_monthly_token_budget = %s WHERE user_id = %s",
                (budget, user_id),
            )

    def get_or_create_conversation(
        self, user_id: int, conversation_id: Optional[int]
    ) -> int:
        """Resolve a conversation id, creating one when absent.

        An id belonging to another account resolves to a new conversation rather
        than raising, so a stale browser tab cannot probe for someone else's
        thread.
        """
        now = utc_now_iso()
        with get_connection() as conn:
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
        with get_connection() as conn:
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
                    utc_now_iso(),
                ),
            )

    def history(self, conversation_id: int, user_id: int, turns: int) -> List[Dict]:
        """The last ``turns`` exchanges, oldest first.

        Counts exchanges rather than rows, hence the doubled limit. This is the
        main control on how input cost grows over a long conversation.
        """
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT role, content, actions FROM chat_messages"
                " WHERE conversation_id = %s AND user_id = %s"
                " ORDER BY message_id DESC LIMIT %s",
                (conversation_id, user_id, max(2, turns * 2)),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def list_conversations(self, user_id: int, limit: int = 20) -> List[Dict]:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT conversation_id, title, created_at, updated_at"
                " FROM chat_conversations WHERE user_id = %s"
                " ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_title(self, conversation_id: int, user_id: int, title: str) -> None:
        """Only the first message names a conversation."""
        with get_connection() as conn:
            conn.execute(
                "UPDATE chat_conversations SET title = %s"
                " WHERE conversation_id = %s AND user_id = %s AND title IS NULL",
                (title[:120], conversation_id, user_id),
            )

    def delete_conversation(self, conversation_id: int, user_id: int) -> bool:
        with get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM chat_conversations WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, user_id),
            )
            return cur.rowcount > 0

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
        with get_connection() as conn:
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
                    utc_now_iso(),
                ),
            )

    def tokens_this_month(self, user_id: int) -> int:
        """Total billable tokens this calendar month, for budget enforcement.

        Cached reads count. They cost a tenth as much but are still consumption,
        and ignoring them would drift from the cost chart beside it.
        """
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(input_tokens + output_tokens + cached_tokens), 0) AS n"
                " FROM chat_usage WHERE user_id = %s AND created_at >= %s",
                (user_id, _month_start()),
            ).fetchone()
        return int(row["n"] or 0)

    def usage_overview(self, days: int = 30) -> Dict:
        """Aggregates for the admin Lumi tab."""
        since = (utc_now() - timedelta(days=days)).isoformat()
        with get_connection() as conn:
            totals = dict(conn.execute(
                "SELECT COALESCE(SUM(input_tokens) FILTER (WHERE created_at >= %(since)s), 0) AS input_tokens,"
                "       COALESCE(SUM(output_tokens) FILTER (WHERE created_at >= %(since)s), 0) AS output_tokens,"
                "       COALESCE(SUM(cached_tokens) FILTER (WHERE created_at >= %(since)s), 0) AS cached_tokens,"
                "       COALESCE(SUM(cost_micros) FILTER (WHERE created_at >= %(since)s), 0) AS cost_micros,"
                "       COUNT(*) FILTER (WHERE created_at >= %(since)s) AS calls,"
                "       COALESCE(SUM(cost_micros) FILTER (WHERE created_at >= %(month)s), 0) AS month_cost_micros"
                " FROM chat_usage WHERE created_at >= LEAST(%(since)s::text, %(month)s::text)",
                {"since": since, "month": _month_start()},
            ).fetchone())
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
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM accounts WHERE chat_enabled = TRUE) AS granted,"
                "       (SELECT COUNT(*) FROM chat_conversations) AS conversations"
            ).fetchone()

        month_cost = totals.pop("month_cost_micros")
        return {
            "window_days": days,
            "totals": totals,
            "per_user": [dict(r) for r in per_user],
            "month_to_date_cost_micros": int(month_cost or 0),
            "users_granted": int(counts["granted"] or 0),
            "conversations": int(counts["conversations"] or 0),
        }

    def prune(self) -> Dict:
        """Drop stale conversations and ancient ledger rows.

        Scheduled on the sync replica only, so it runs once per cluster rather
        than once per container. Messages cascade with their conversation.
        """
        conv_cutoff = (utc_now() - timedelta(days=CONVERSATION_TTL_DAYS)).isoformat()
        usage_cutoff = (utc_now() - timedelta(days=USAGE_TTL_DAYS)).isoformat()
        with get_connection() as conn:
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


store = ChatStore()
