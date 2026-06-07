"""
Ko-fi supporters API — webhook ingest + the public "Lumi's Loved Mortals" list.

Two halves:

  * ``POST /kofi/webhook`` — the URL you paste into Ko-fi (Settings → Advanced →
    Webhooks). Ko-fi POSTs ``application/x-www-form-urlencoded`` with a single
    ``data`` field holding the event JSON. We verify the shared
    ``verification_token`` against ``KOFI_VERIFICATION_TOKEN`` and append the
    event to the ledger (idempotent). NOT for the frontend — only Ko-fi calls it.
  * ``GET /supporters`` / ``GET /supporters/stats`` — public, unauthenticated,
    read-only. The frontend renders the loved-mortals page from these.

Expand vs. shrink: a new payment grows the list automatically. Ko-fi sends no
cancellation event, so the list "shrinks" by inference — a *subscriber* is only
listed while their last payment is within ``KOFI_ACTIVE_WINDOW_DAYS`` (default
35, i.e. one billing cycle + grace); one-time tippers are kept forever. Pass
``?include_lapsed=true`` to show everyone regardless.
"""

import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Query, Request

from .db import SupporterStore

router = APIRouter(tags=["supporters"])
store = SupporterStore()

# How long after their last payment a *subscriber* still counts as active.
# One-time tippers ignore this (they're kept forever).
_ACTIVE_WINDOW_DAYS = int(os.getenv("KOFI_ACTIVE_WINDOW_DAYS", "35"))

# Tiny in-process TTL cache for the public list — a fan page can get bursty
# traffic and the aggregation, while cheap, doesn't need to run per request. Held
# per replica (no cross-replica coordination needed; each just refreshes lazily).
_CACHE_TTL = int(os.getenv("KOFI_LIST_CACHE_TTL", "60"))
_cache: Dict[str, object] = {"at": 0.0, "rows": None}
_cache_lock = threading.Lock()


def _verification_token() -> Optional[str]:
    return os.getenv("KOFI_VERIFICATION_TOKEN")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_active(row: Dict, cutoff: datetime) -> bool:
    """A subscriber is active only if their last payment is newer than the cutoff;
    one-time supporters are always active. Unparseable timestamps fail open
    (treated as active) so a Ko-fi format change never silently hides supporters."""
    if not row.get("is_subscription"):
        return True
    ts = row.get("last_payment_at")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return True
    return last >= cutoff


def _cached_rows() -> List[Dict]:
    """All public supporters (unfiltered aggregate), refreshed at most every
    ``_CACHE_TTL`` seconds."""
    now = time.monotonic()
    with _cache_lock:
        rows = _cache["rows"]
        if rows is not None and (now - float(_cache["at"])) < _CACHE_TTL:
            return rows  # type: ignore[return-value]
    rows = store.list_supporters()
    with _cache_lock:
        _cache["rows"] = rows
        _cache["at"] = now
    return rows


def _public_view(row: Dict) -> Dict:
    """Shape one aggregated supporter for the frontend (no PII)."""
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


# --- webhook ingest (called by Ko-fi, not the frontend) --------------------
@router.post("/kofi/webhook")
async def kofi_webhook(request: Request):
    """Receive a Ko-fi payment webhook and append it to the ledger.

    Ko-fi sends ``application/x-www-form-urlencoded`` with one ``data`` field
    whose value is the event JSON (including a ``verification_token`` we match
    against ``KOFI_VERIFICATION_TOKEN``). Always answers 200 on a duplicate so
    Ko-fi stops retrying; rejects a bad/absent token with 401."""
    expected = _verification_token()
    if not expected:
        # Fail closed: without a configured token we can't trust any caller.
        raise HTTPException(status_code=503, detail="Ko-fi webhook not configured")

    # Ko-fi posts application/x-www-form-urlencoded with one ``data`` field. Parse
    # the raw body ourselves rather than request.form() so we don't pull in the
    # python-multipart dependency Starlette's form parser now requires.
    body = (await request.body()).decode("utf-8", "replace")
    values = parse_qs(body).get("data")
    raw = values[0] if values else None
    if not raw:
        raise HTTPException(status_code=400, detail="Missing 'data' field")

    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed 'data' JSON")

    token = event.get("verification_token") or ""
    if not secrets.compare_digest(str(token), expected):
        raise HTTPException(status_code=401, detail="Invalid verification token")

    inserted = store.record_transaction(event)
    if inserted:
        # New money in → drop the cached list so the page reflects it promptly.
        with _cache_lock:
            _cache["rows"] = None
    return {"success": True, "recorded": inserted}


# --- public read endpoints (the frontend) ----------------------------------
@router.get("/supporters")
async def list_supporters(
    include_lapsed: bool = Query(
        False, description="Include subscribers whose membership has lapsed."),
    limit: Optional[int] = Query(
        None, ge=1, le=1000, description="Cap the number of supporters returned."),
):
    """Public list for the 'Lumi's Loved Mortals' page. No auth. Most-recent
    payment first. Lapsed subscribers are hidden unless ``include_lapsed=true``."""
    rows = _cached_rows()
    cutoff = _now() - timedelta(days=_ACTIVE_WINDOW_DAYS)
    if not include_lapsed:
        rows = [r for r in rows if _is_active(r, cutoff)]
    supporters = [_public_view(r) for r in rows]
    if limit is not None:
        supporters = supporters[:limit]
    return {"success": True, "count": len(supporters), "supporters": supporters}


@router.get("/supporters/stats")
async def supporters_stats():
    """Aggregate totals for a page header (e.g. 'N loved mortals • X brewed').

    Sums over *active* supporters (same rule as /supporters). ``total_raised`` is
    a naive cross-currency sum; ``currency`` is the most common one seen."""
    rows = _cached_rows()
    cutoff = _now() - timedelta(days=_ACTIVE_WINDOW_DAYS)
    active = [r for r in rows if _is_active(r, cutoff)]

    total_raised = round(sum((r.get("total_amount") or 0) for r in active), 2)
    currencies: Dict[str, int] = {}
    for r in active:
        c = r.get("currency")
        if c:
            currencies[c] = currencies.get(c, 0) + 1
    currency = max(currencies, key=currencies.get) if currencies else None

    return {
        "success": True,
        "supporter_count": len(active),
        "total_raised": total_raised,
        "currency": currency,
    }
