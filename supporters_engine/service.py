"""The public supporter list: a per-replica cache over the ledger, the rule that
hides lapsed subscribers, and the header totals.

Ko-fi sends no cancellation event, so a subscriber counts as active only while
their last payment is within KOFI_ACTIVE_WINDOW_DAYS (one billing cycle plus
grace). One-time supporters stay listed forever.
"""

import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from core.clock import utc_now
from core.config import get_settings

from .db import store

_lock = threading.Lock()
_rows: Optional[List[Dict]] = None
_fetched_at = 0.0
# Bumped by every invalidation, so a fetch that started before a new payment
# landed cannot write its stale result back over the invalidation.
_generation = 0


def _cached_rows() -> List[Dict]:
    global _rows, _fetched_at
    with _lock:
        if _rows is not None and time.monotonic() - _fetched_at < get_settings().kofi_list_cache_ttl:
            return _rows
        generation = _generation
    rows = store.list_supporters()
    with _lock:
        if generation == _generation:
            _rows, _fetched_at = rows, time.monotonic()
    return rows


def _invalidate() -> None:
    global _rows, _generation
    with _lock:
        _generation += 1
        _rows = None


def _is_active(row: Dict, cutoff: datetime) -> bool:
    """Unparseable timestamps count as active, so a Ko-fi format change never
    silently hides supporters."""
    if not row.get("is_subscription"):
        return True
    ts = row.get("last_payment_at")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last >= cutoff


def _active(rows: List[Dict]) -> List[Dict]:
    cutoff = utc_now() - timedelta(days=get_settings().kofi_active_window_days)
    return [r for r in rows if _is_active(r, cutoff)]


def _public_view(row: Dict) -> Dict:
    return {
        "name": row.get("from_name") or "Anonymous",
        "message": row.get("message"),
        "total_amount": row.get("total_amount"),
        "currency": row.get("currency"),
        "is_subscription": bool(row.get("is_subscription")),
        "tier_name": row.get("tier_name"),
        "type": row.get("type"),
        "contribution_count": row.get("contribution_count"),
        "first_seen_at": row.get("first_seen_at"),
        "last_payment_at": row.get("last_payment_at"),
    }


def record_payment(event: Dict) -> bool:
    """Append a webhook event to the ledger. False for a Ko-fi retry."""
    inserted = store.record_transaction(event)
    if inserted:
        _invalidate()
    return inserted


def list_public(include_lapsed: bool, limit: Optional[int]) -> List[Dict]:
    rows = _cached_rows()
    if not include_lapsed:
        rows = _active(rows)
    return [_public_view(r) for r in rows[:limit]]


def stats() -> Dict:
    """Totals over active supporters. ``total_raised`` is a naive cross-currency
    sum; ``currency`` is the most common one."""
    active = _active(_cached_rows())
    currencies = Counter(r["currency"] for r in active if r.get("currency"))
    return {
        "supporter_count": len(active),
        "total_raised": round(sum((r.get("total_amount") or 0) for r in active), 2),
        "currency": currencies.most_common(1)[0][0] if currencies else None,
    }
