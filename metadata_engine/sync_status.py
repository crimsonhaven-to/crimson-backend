"""Live status of the Fribb mapping resync — the tiny shared state /health reads.

The initial mapping sync used to be ``await``-ed inside the lifespan *before* the
app started serving, so a cold boot that needed a rebuild blocked uvicorn (and the
/health probe) for the whole multi-minute Fribb download + AniList enrichment. It
now runs as a background task (see ``api.py``'s lifespan), so the app comes up
immediately and this module records where that background sync is up to.

Thread-safe: the sync runs in a worker thread (``run_in_threadpool`` ->
``asyncio.run``) while /health reads the snapshot from the event loop, so every
access takes the lock. Nothing here touches the DB or the network.
"""

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# phase transitions:
#   disabled     -> this replica has RUN_DB_SYNC off; it never syncs
#   running      -> the background initial sync is in flight (checking + maybe rebuilding)
#   up_to_date   -> ETag matched a non-empty DB; nothing was rebuilt
#   done         -> the mapping tables were rebuilt from Fribb
#   failed       -> the sync raised / rolled back (the previous snapshot is intact)
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
    """Record a phase transition. ``started``/``finished`` stamp the timestamps."""
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
