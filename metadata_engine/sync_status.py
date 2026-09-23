"""Where the startup mapping sync has got to, for /health. The sync runs in the
background so the app comes up at once even when a cold boot needs a full
rebuild; it runs in a worker thread while /health reads from the event loop,
hence the lock.
"""

import threading
from typing import Any, Dict, Optional

from core.clock import utc_now_iso

# phases:
#   idle         the sync has not started yet
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


def set_phase(
    phase: str,
    detail: Optional[str] = None,
    *,
    started: bool = False,
    finished: bool = False,
) -> None:
    with _lock:
        _state["phase"] = phase
        _state["detail"] = detail
        if started:
            _state["started_at"] = utc_now_iso()
            _state["finished_at"] = None
        if finished:
            _state["finished_at"] = utc_now_iso()


def snapshot() -> Dict[str, Any]:
    with _lock:
        return dict(_state)
