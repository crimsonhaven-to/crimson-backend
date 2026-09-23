"""Ko-fi payment ledger behind the public supporters page.

Ko-fi has no "list my supporters" API; it only pushes a webhook per payment and
never one for a cancellation. So every event is appended to
``kofi_transactions`` and the supporter list is aggregated on read. A ledger
rather than one row per supporter because Ko-fi retries webhooks (the
transaction id as primary key makes a replay a no-op) and every subscription
renewal is a new transaction (grouping by supporter turns them into one
supporter whose ``last_payment_at`` advances).

``email`` is only a server-side identity key: it is never returned by
list_supporters, and only events the supporter marked public are aggregated.
"""

import logging
from typing import Dict, List, Optional

from core.clock import utc_now_iso
from core.db_pool import get_connection, lock_schema_init

logger = logging.getLogger(__name__)


def _supporter_key(email: Optional[str], from_name: Optional[str],
                   transaction_id: str) -> str:
    """Stable identity across payments: email is the most durable, then the
    display name. The transaction id keeps an anonymous one-off as its own row
    instead of merging unrelated people."""
    if email and email.strip():
        return "email:" + email.strip().lower()
    if from_name and from_name.strip():
        return "name:" + from_name.strip().lower()
    return "txn:" + transaction_id


class SupporterStore:
    def init_db(self) -> None:
        with get_connection() as conn:
            lock_schema_init(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kofi_transactions (
                    kofi_transaction_id TEXT PRIMARY KEY,
                    message_id          TEXT,
                    supporter_key       TEXT NOT NULL,
                    type                TEXT,
                    from_name           TEXT,
                    message             TEXT,
                    amount              NUMERIC,
                    currency            TEXT,
                    is_public           BOOLEAN NOT NULL DEFAULT TRUE,
                    is_subscription     BOOLEAN NOT NULL DEFAULT FALSE,
                    tier_name           TEXT,
                    email               TEXT,
                    kofi_timestamp      TEXT,
                    received_at         TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kofi_supporter
                    ON kofi_transactions(supporter_key);
                CREATE INDEX IF NOT EXISTS idx_kofi_public_ts
                    ON kofi_transactions(is_public, kofi_timestamp);
                """
            )
        logger.info("Supporters schema ready")

    def record_transaction(self, event: Dict) -> bool:
        """Append one decoded Ko-fi event. False if this transaction id was
        already recorded (a Ko-fi retry)."""
        transaction_id = (event.get("kofi_transaction_id")
                          or event.get("message_id") or "").strip()
        if not transaction_id:
            # Without an id there is no dedup, but the payment still lands.
            transaction_id = "ts:" + (event.get("timestamp") or utc_now_iso())

        email = event.get("email")
        from_name = event.get("from_name")
        is_subscription = bool(event.get("is_subscription_payment")) or \
            (event.get("type") == "Subscription")

        row = {
            "kofi_transaction_id": transaction_id,
            "message_id": event.get("message_id"),
            "supporter_key": _supporter_key(email, from_name, transaction_id),
            "type": event.get("type"),
            "from_name": from_name,
            "message": event.get("message"),
            "amount": _parse_amount(event.get("amount")),
            "currency": event.get("currency"),
            "is_public": bool(event.get("is_public", True)),
            "is_subscription": is_subscription,
            "tier_name": event.get("tier_name"),
            "email": email,
            "kofi_timestamp": event.get("timestamp"),
            "received_at": utc_now_iso(),
        }

        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO kofi_transactions
                    (kofi_transaction_id, message_id, supporter_key, type, from_name,
                     message, amount, currency, is_public, is_subscription, tier_name,
                     email, kofi_timestamp, received_at)
                VALUES
                    (%(kofi_transaction_id)s, %(message_id)s, %(supporter_key)s, %(type)s,
                     %(from_name)s, %(message)s, %(amount)s, %(currency)s, %(is_public)s,
                     %(is_subscription)s, %(tier_name)s, %(email)s, %(kofi_timestamp)s,
                     %(received_at)s)
                ON CONFLICT (kofi_transaction_id) DO NOTHING
                """,
                row,
            )
            return cur.rowcount > 0

    def list_supporters(self) -> List[Dict]:
        """One row per public supporter, most recent payment first, with the
        display fields from their latest payment. ``total_amount`` is a naive sum,
        so a supporter who paid in two currencies shows the latest one's symbol
        over a mixed sum, which is fine for a fan list."""
        with get_connection() as conn:
            rows = conn.execute(
                """
                WITH agg AS (
                    SELECT supporter_key,
                           SUM(amount)::float       AS total_amount,
                           MIN(kofi_timestamp)      AS first_seen_at,
                           MAX(kofi_timestamp)      AS last_payment_at,
                           bool_or(is_subscription) AS is_subscription,
                           COUNT(*)                 AS contribution_count
                    FROM kofi_transactions
                    WHERE is_public = TRUE
                    GROUP BY supporter_key
                ),
                latest AS (
                    SELECT DISTINCT ON (supporter_key)
                           supporter_key, from_name, message, currency, tier_name, type
                    FROM kofi_transactions
                    WHERE is_public = TRUE
                    ORDER BY supporter_key, kofi_timestamp DESC NULLS LAST
                )
                SELECT a.supporter_key, a.total_amount, a.first_seen_at,
                       a.last_payment_at, a.is_subscription, a.contribution_count,
                       l.from_name, l.message, l.currency, l.tier_name, l.type
                FROM agg a JOIN latest l USING (supporter_key)
                ORDER BY a.last_payment_at DESC NULLS LAST
                """
            ).fetchall()
            return [dict(r) for r in rows]


def _parse_amount(raw) -> Optional[float]:
    """Ko-fi sends amounts as strings like ``"3.00"``."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


store = SupporterStore()
