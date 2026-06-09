"""
Ko-fi supporters storage (PostgreSQL) — the ledger behind "Lumi's Loved Mortals".

Ko-fi has no "list my supporters" REST API; it only *pushes* a webhook to us when
a payment happens (a tip, a membership/subscription payment, a commission or a
shop order). It also never fires when a subscription is cancelled. So this store
keeps an append-only ledger of every payment event Ko-fi sends us
(``kofi_transactions``) and derives the public supporter list by aggregating that
ledger on read (see :meth:`SupporterStore.list_supporters`).

Why a ledger instead of one row per supporter:

  * **Idempotency** — Ko-fi retries a webhook until it gets a 2xx, so the same
    event can arrive several times. ``kofi_transaction_id`` is the primary key, so
    a replay is a no-op INSERT and never double-counts a contribution.
  * **Subscriptions** — every monthly renewal is a *new* transaction id. Grouping
    the ledger by a stable supporter key turns those into a single supporter whose
    ``last_payment_at`` advances each renewal — which is exactly what powers the
    "active vs. lapsed" filtering the public endpoint does (Ko-fi gives us no
    cancellation event, so a lapsed subscriber is inferred from a stale
    ``last_payment_at``).

Storage is the shared PostgreSQL pool (see db_pool), same as the account engine.
Access is synchronous psycopg, called from the async handlers via FastAPI's
thread pool — the volumes here are tiny.

Privacy: ``email`` is stored only as a server-side identity key (Ko-fi sends it
for the seller's own records); it is NEVER returned by :meth:`list_supporters`
and so never reaches the public page. Only events the supporter marked public
(``is_public``) are aggregated into the list at all.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from db_pool import get_connection, lock_schema_init


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supporter_key(email: Optional[str], from_name: Optional[str],
                   transaction_id: str) -> str:
    """Stable identity for a supporter across multiple payments.

    Email is the most durable identity Ko-fi gives us, so prefer it; fall back to
    the display name, and finally to the transaction id (which keeps a truly
    anonymous one-off as its own row instead of merging unrelated people)."""
    if email and email.strip():
        return "email:" + email.strip().lower()
    if from_name and from_name.strip():
        return "name:" + from_name.strip().lower()
    return "txn:" + transaction_id


class SupporterStore:
    """Thin PostgreSQL data layer for the Ko-fi supporters ledger."""

    def init_db(self) -> None:
        """Create the schema (idempotent — safe on every replica/boot)."""
        with get_connection() as conn:
            # Serialize DDL across replicas (see db_pool.lock_schema_init).
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
        print("[SupporterStore] Schema ready (PostgreSQL).")

    def record_transaction(self, event: Dict) -> bool:
        """Persist one Ko-fi webhook event.

        ``event`` is the decoded Ko-fi payload (the JSON inside the form ``data``
        field). Returns ``True`` if a new row was inserted, ``False`` if this
        transaction id was already recorded (a Ko-fi retry) — letting the caller
        stay idempotent while still answering 200 so Ko-fi stops retrying.
        """
        transaction_id = (event.get("kofi_transaction_id")
                          or event.get("message_id") or "").strip()
        if not transaction_id:
            # Without an id we can't dedup; synthesize one from the timestamp so the
            # row still lands rather than being dropped.
            transaction_id = "ts:" + (event.get("timestamp") or _now_iso())

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
            "received_at": _now_iso(),
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
        """One aggregated row per public supporter, most-recent payment first.

        Aggregates the ledger (public events only): total contributed, first/last
        payment, whether they're a subscriber, and the display fields (name,
        message, tier, currency) taken from their most recent payment. ``email``
        is intentionally excluded. Active-vs-lapsed filtering and limiting are
        applied by the route layer so this stays a pure read.

        Note: ``total_amount`` is a naive sum across the supporter's payments; if a
        supporter ever paid in multiple currencies it reflects the latest
        currency's symbol over a mixed sum. For a fan list this is fine.
        """
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
    """Ko-fi sends amounts as strings like ``"3.00"``; coerce to float."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
