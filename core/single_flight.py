"""Request coalescing for cache misses: one upstream call per key at a time.

On a cold or just-expired key every concurrent request would otherwise miss both
cache tiers and hit the upstream at once. AniList is the worst case: its entries
expire on ``nextAiringEpisode``, so a popular title's cache lapses at peak
traffic, and the 429 backoff then stalls every request caught in the stampede.

Per process, not per cluster: three replicas still make three upstream calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, TypeVar

logger = logging.getLogger("crimson.single_flight")

T = TypeVar("T")

# Holds only keys with a fetch in flight; each entry is removed when its task
# settles, so this cannot grow past the concurrent miss count.
_inflight: Dict[str, asyncio.Task] = {}


def _release(key: str, task: asyncio.Task) -> None:
    _inflight.pop(key, None)
    # Every waiter may have been cancelled while the shielded task kept running,
    # leaving a raised exception nobody consumed. Retrieving it here keeps
    # asyncio from reporting it as never retrieved at garbage-collection time.
    if not task.cancelled():
        task.exception()


async def run(key: str, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Await ``factory()``, sharing one in-flight call per ``key``. A failure
    reaches every waiter and leaves the key clean, so the next request retries
    rather than inheriting a cached rejection."""
    task = _inflight.get(key)
    if task is None:
        # The task inherits the leader's context, so request-scoped state such as
        # the request id stays bound for the duration of the shared fetch.
        task = asyncio.get_running_loop().create_task(factory())
        _inflight[key] = task
        task.add_done_callback(lambda t: _release(key, t))
    else:
        logger.debug("joined in-flight fetch for %s", key)

    # Without the shield one client disconnecting would cancel the fetch every
    # other waiter relies on. The task still fills the cache if everyone leaves.
    return await asyncio.shield(task)
