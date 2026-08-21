"""Live status of the Fribb mapping resync: the shared state /health reads.

The initial sync runs as a background task rather than blocking the lifespan, so
the app comes up immediately even on a cold boot that needs a full rebuild. This
records where that sync has got to.

Thread-safe: the sync runs in a worker thread while /health reads from the event
loop, so every access takes the lock. Nothing here touches the DB or network.
"""

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# phases:
#   disabled     RUN_DB_SYNC is off on this replica, so it never syncs
#   running      the background initial sync is in flight
#   up_to_date   the ETag matched a non-empty DB, so nothing was rebuilt
#   done         the mapping tables were rebuilt from Fribb
#   failed       the sync rolled back; the previous snapshot is intact
_lock = threading.Lock()
_state: Dict[str, Any] = {
    "phase": "idle",
    "detail": None,
    "started_at": None,
    "finished_at": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_phase(
    phase: str,
    detail: Optional[str] = None,
    *,
    started: bool = False,
    finished: bool = False,
) -> None:
    """Record a phase transition; ``started``/``finished`` stamp the timestamps."""
    with _lock:
        _state["phase"] = phase
        _state["detail"] = detail
        if started:
            _state["started_at"] = _now()
            _state["finished_at"] = None
        if finished:
            _state["finished_at"] = _now()


def snapshot() -> Dict[str, Any]:
    """A copy of the current status, safe to serialize into /health."""
    with _lock:
        return dict(_state)
