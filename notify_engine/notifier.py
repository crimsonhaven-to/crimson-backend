"""
The two jobs behind the airing surface: refresh the schedule, then notify.

Split in two because they want different intervals for different reasons. The
schedule changes rarely (a broadcast slipping a week), so refreshing it often
would spend AniList requests to learn nothing. A notification wants to go out
soon after the episode airs, so that half runs frequently and touches only the
database.

Both are pinned to the ``RUN_DB_SYNC`` replica (see startup.py). The claim in
``AiringStore.claim`` makes concurrent replicas *correct* rather than
duplicate-sending, but three of them each opening an SMTP connection and racing
for claims is waste, and it makes the log unreadable. The claim stays regardless:
it is what makes a misconfigured second replica harmless instead of an incident.
"""

from __future__ import annotations

import logging

import httpx

from account_engine import mailer
from core.config import Config

from .db import store
from .schedule import fetch_window

logger = logging.getLogger("crimson.airing")

# How far back a just-aired episode still counts as news. Wider than the notify
# interval by a large margin, so an outage of a few hours catches up rather than
# silently dropping everything it missed; short enough that enabling the feature,
# or verifying an email today, cannot produce a backlog blast.
LOOKBACK_HOURS = 36

# How far ahead the calendar is filled.
HORIZON_DAYS = 7

# The most notices one tick will send. A popular seasonal title can notify every
# one of its subscribers at once; this bounds a single SMTP session, and whatever
# is left is picked up on the next tick, still inside the lookback.
MAX_PER_RUN = 200


async def refresh_schedule() -> int:
    """Pull the airing window from AniList into ``airing_schedule``.

    Runs on a fresh event loop in a scheduler thread, so it cannot borrow the
    shared client, which is bound to the main loop."""
    async with httpx.AsyncClient(timeout=Config.REQUEST_TIMEOUT) as client:
        rows = await fetch_window(client, LOOKBACK_HOURS, HORIZON_DAYS)
    if not rows:
        logger.warning("Airing refresh returned nothing; keeping the previous window")
        return 0
    return store.upsert_schedule(rows)


def send_due_notifications() -> dict:
    """Claim and send every notification that has come due.

    Synchronous: it is called from an APScheduler worker thread, and both the
    database and SMTP are blocking anyway.
    """
    result = {"claimed": 0, "sent": 0, "failed": 0, "skipped": 0}

    pending = store.pending_notifications(LOOKBACK_HOURS, MAX_PER_RUN)
    if not pending:
        return result

    dry_run = Config.AIRING_NOTIFY_DRY_RUN
    base = mailer.frontend_base_url()

    messages = []
    for row in pending:
        # Claim FIRST. If the process dies here the subscriber misses one
        # notice; claiming after a send would instead resend it on every tick
        # forever. See migrations/004_airing.sql for the full reasoning.
        if not store.claim(row["user_id"], row["anilist_id"], row["episode"]):
            result["skipped"] += 1  # another worker already owns it
            continue
        result["claimed"] += 1

        title = row["title"] or "A title you follow"
        text, html_body = mailer.airing_bodies(
            title, row["episode"], row["username"],
            f"{base}/anime/{row['anilist_id']}",
        )
        messages.append({
            "email": row["email"],
            "subject": f"{title} - episode {row['episode']} has aired",
            "text": text,
            "html": html_body,
            "_user_id": row["user_id"],
            "_anilist_id": row["anilist_id"],
            "_episode": row["episode"],
        })

    if dry_run:
        # The claim still happened, so a dry run is not repeatable against the
        # same rows. That is deliberate: it exercises the real claim path, which
        # is the part worth rehearsing.
        for message in messages:
            logger.info(
                "[airing][dry-run] would notify %s about anilist=%s episode=%s",
                message["email"], message["_anilist_id"], message["_episode"],
            )
            store.record_outcome(message["_user_id"], message["_anilist_id"],
                                 message["_episode"], sent=False)
        result["skipped"] += len(messages)
        return result

    def _record(message, ok):
        store.record_outcome(message["_user_id"], message["_anilist_id"],
                             message["_episode"], sent=ok)

    outcome = mailer.send_airing_batch(messages, progress=_record)
    result["sent"] = outcome["sent"]
    result["failed"] = outcome["failed"]
    return result
