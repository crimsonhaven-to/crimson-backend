"""
Request coalescing for cache misses.

The two-tier cache in :mod:`core.response_cache` is only consulted by the
caller; the check-then-fetch lives at each call site. So on a cold or
just-expired key every concurrent request misses L1, misses L2 and calls the
upstream at the same moment. AniList is the worst case: its entries carry
``nextAiringEpisode``, so a popular title's cache expires precisely while that
title is at peak traffic, and the 429 ladder in ``metadata_engine.anilist`` then
turns one stampede into a multi-second stall for every request caught in it.

:func:`run` collapses that to one upstream call per key. The first caller runs
the fetch, everyone else awaits the same task.

Per process, not per cluster: with three replicas a stampede collapses to three
upstream calls rather than one. Closing that last gap needs the shared Redis
that ``core.rate_limit`` is also waiting on.
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
    """Await ``factory()``, sharing one in-flight call per ``key``.

    ``factory`` is invoked only for the first caller. A failure propagates to
    everyone waiting on it and leaves the key clean, so the next request retries
    rather than inheriting a cached rejection.
    """
    task = _inflight.get(key)
    if task is None:
        # The task inherits the leader's context, so request-scoped state such as
        # the request id stays bound for the duration of the shared fetch.
        task = asyncio.get_running_loop().create_task(factory())
        _inflight[key] = task
        task.add_done_callback(lambda t: _release(key, t))
    else:
        logger.debug("joined in-flight fetch for %s", key)

    # Shielded: awaiting a task propagates the awaiter's cancellation into it, so
    # one client disconnecting would otherwise abort the fetch every other waiter
    # is relying on. The task runs to completion and still populates the cache
    # even if every caller walks away.
    return await asyncio.shield(task)
