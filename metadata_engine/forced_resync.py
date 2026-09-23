"""An admin-triggered mapping rebuild, run in-process on the live engine so it
gets the warm pool and one MVCC-safe transaction, with state the dashboard polls.
The command-line twin is ``python -m metadata_engine.resync``."""

import asyncio
from typing import Any, Dict

from core.background import spawn
from core.clock import utc_now_iso

from .mapping_sync import engine

# Returned by reference to the status endpoint, so it is only ever updated.
state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "error": None,
    "triggered_by": None,
}
# A second trigger waits instead of starting a second Fribb download.
_lock = asyncio.Lock()


async def _run(triggered_by: str) -> None:
    async with _lock:
        state.update(
            running=True,
            started_at=utc_now_iso(),
            finished_at=None,
            ok=None,
            error=None,
            triggered_by=triggered_by,
        )
        try:
            outcome = await engine.sync_database_async(force=True)
            state.update(ok=outcome == "synced", error=None if outcome == "synced" else outcome)
        except Exception as e:
            state.update(ok=False, error=str(e))
        finally:
            state.update(running=False, finished_at=utc_now_iso())


def start(triggered_by: str) -> bool:
    """False when one is already running."""
    if state["running"]:
        return False
    spawn(_run(triggered_by))
    return True
