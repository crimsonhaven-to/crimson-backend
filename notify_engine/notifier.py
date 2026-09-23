"""The two airing jobs: refresh the schedule, then notify.

Split because they want different intervals. The schedule changes rarely (a
broadcast slipping a week), so refreshing it often spends AniList requests to
learn nothing; a notice should go out soon after the episode airs, so that half
runs often and touches only the database.

Both run on the ``RUN_DB_SYNC`` replica only. The ledger claim would keep a
second replica from double-sending anyway, but several replicas each opening an
SMTP session and racing for claims is waste. The claim stays so that a
misconfigured second replica is harmless instead of an incident.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import httpx

from account_engine import mailer
from core.config import get_settings
from core.http_client import REQUEST_TIMEOUT

from .db import LedgerKey, store
from .schedule import fetch_window

logger = logging.getLogger("crimson.airing")

# How far back a just-aired episode still counts as news. Far wider than the
# notify interval, so a few hours of outage catches up instead of silently
# dropping what it missed; short enough that enabling the feature, or verifying
# an email today, cannot produce a backlog blast.
LOOKBACK_HOURS = 36

HORIZON_DAYS = 7

# Bounds one SMTP session when a popular title notifies all its subscribers at
# once. The rest goes out on the next tick, still inside the lookback.
MAX_PER_RUN = 200


async def refresh_schedule() -> int:
    """Pull the airing window from AniList into ``airing_schedule``."""
    # Runs on the scheduler's own event loop, where the shared client cannot go.
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        rows = await fetch_window(client, LOOKBACK_HOURS, HORIZON_DAYS)
    if not rows:
        logger.warning("Airing refresh returned nothing; keeping the previous window")
        return 0
    return store.upsert_schedule(rows)


def _key(row: dict) -> LedgerKey:
    return (row["user_id"], row["anilist_id"], row["episode"])


def send_due_notifications() -> dict:
    """Claim, send and record every notification that has come due. Synchronous:
    it runs in a scheduler thread and both the database and SMTP block anyway."""
    result = {"claimed": 0, "sent": 0, "failed": 0, "skipped": 0}

    pending = store.pending_notifications(LOOKBACK_HOURS, MAX_PER_RUN)
    if not pending:
        return result

    # Claim before sending. If the process dies after the claim the subscriber
    # misses one notice; claiming after the send would resend it on every tick.
    won = store.claim([_key(row) for row in pending])
    rows = [row for row in pending if _key(row) in won]
    result["claimed"] = len(rows)
    result["skipped"] = len(pending) - len(rows)

    if get_settings().airing_notify_dry_run:
        # The claim is real, so a dry run cannot be repeated against the same rows.
        # Deliberate: the claim path is the part worth rehearsing.
        for row in rows:
            logger.info("[dry-run] would notify %s about anilist=%s episode=%s",
                        row["email"], row["anilist_id"], row["episode"])
        store.record_outcomes([(_key(row), False) for row in rows])
        result["skipped"] += len(rows)
        return result

    base = mailer.frontend_base_url()
    messages = []
    key_by_message = {}
    for row in rows:
        title = row["title"] or "A title you follow"
        text, html_body = mailer.airing_bodies(
            title, row["episode"], row["username"], f"{base}/anime/{row['anilist_id']}",
        )
        message = {
            "email": row["email"],
            "subject": f"{title} - episode {row['episode']} has aired",
            "text": text,
            "html": html_body,
        }
        messages.append(message)
        key_by_message[id(message)] = _key(row)

    outcomes: List[Tuple[LedgerKey, bool]] = []
    try:
        sent = mailer.send_airing_batch(
            messages, progress=lambda m, ok: outcomes.append((key_by_message[id(m)], ok))
        )
    finally:
        store.record_outcomes(outcomes)
    result["sent"] = sent["sent"]
    result["failed"] = sent["failed"]
    return result
