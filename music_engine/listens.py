"""Recording what a member listened to. The player decides what counts as a
listen (30 seconds of real playing); this only keeps a report plausible."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.clock import utc_now

from . import listens_db

# A device reports songs played offline when it is next online, which for a
# phone left in a drawer can be weeks. Older than this is dropped, not guessed.
MAX_AGE = timedelta(days=30)
# A clock a little fast is common; one far ahead is dropped.
MAX_AHEAD = timedelta(minutes=10)
# The player's rule for what counts, applied again here so an old or odd
# client cannot count a skipped song.
MIN_SECONDS = 30.0
# Tracks with no known length (duration_ms 0) are capped at an hour.
UNKNOWN_LENGTH_CAP = 3600.0
# Seeking back, or a buffering stall counted as playing, can add a little over
# the length.
LENGTH_SLACK = 1.1


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def plausible(
    reported: list[tuple[int, datetime, float]], lengths_ms: dict[int, int], now: datetime
) -> list[tuple[int, datetime, float]]:
    """The reports worth keeping, with seconds capped at what the song could
    have played. Unknown tracks and impossible moments are dropped."""
    kept = []
    for track_id, listened_at, seconds in reported:
        if track_id not in lengths_ms or seconds < MIN_SECONDS:
            continue
        moment = _as_utc(listened_at)
        if not (now - MAX_AGE <= moment <= now + MAX_AHEAD):
            continue
        length = lengths_ms[track_id] / 1000.0
        cap = length * LENGTH_SLACK if length > 0 else UNKNOWN_LENGTH_CAP
        kept.append((track_id, moment, min(float(seconds), cap)))
    return kept


def record(user_id: int, reported: list[tuple[int, datetime, float]]) -> int:
    """How many listens were new. The rest were dropped or already recorded."""
    if not reported:
        return 0
    lengths = listens_db.durations(sorted({track_id for track_id, _, _ in reported}))
    return listens_db.insert(user_id, plausible(reported, lengths, utc_now()))
