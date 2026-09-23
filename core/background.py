"""Fire-and-forget coroutines that cannot be garbage-collected mid-run.

The event loop only keeps a weak reference to a task, so a task nobody holds can
vanish before it finishes. ``spawn`` holds it until it is done.
"""

import asyncio
from typing import Coroutine

_running: set[asyncio.Task] = set()


def spawn(coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task
