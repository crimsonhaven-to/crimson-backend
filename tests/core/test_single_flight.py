"""
Coalescing behaviour of core.single_flight.

The point of the module is what happens to the *other* callers while one fetch
is in flight, so every test here runs several callers concurrently against a
factory that counts its own invocations.
"""


import asyncio

import pytest

from core import single_flight


async def _settle():
    """Yield long enough for a finished task's done-callback to run.

    Releasing the key is a done-callback, which asyncio schedules with
    ``call_soon``: one turn resumes the factory and completes the task, the next
    runs the callback.
    """
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _blocking_factory(gate: asyncio.Event, result="value"):
    """A factory that parks until ``gate`` is set, counting its invocations."""
    calls = []

    async def factory():
        calls.append(1)
        await gate.wait()
        return result

    return factory, calls


async def test_concurrent_callers_share_one_fetch():
    gate = asyncio.Event()
    factory, calls = _blocking_factory(gate)

    waiters = [asyncio.create_task(single_flight.run("k", factory)) for _ in range(5)]
    await asyncio.sleep(0)  # let them all reach the shared task
    gate.set()
    results = await asyncio.gather(*waiters)

    assert len(calls) == 1, "the factory must run once for five concurrent callers"
    assert results == ["value"] * 5


async def test_distinct_keys_do_not_share():
    gate = asyncio.Event()
    gate.set()
    factory, calls = _blocking_factory(gate)

    await asyncio.gather(single_flight.run("a", factory), single_flight.run("b", factory))

    assert len(calls) == 2


async def test_key_is_released_after_success():
    gate = asyncio.Event()
    gate.set()
    factory, calls = _blocking_factory(gate)

    await single_flight.run("released", factory)
    await single_flight.run("released", factory)

    # A settled flight must not be reused: the second call is a fresh fetch, not
    # a replay of the first one's result.
    assert len(calls) == 2
    assert "released" not in single_flight._inflight


async def test_failure_reaches_every_waiter_and_leaves_the_key_clean():
    gate = asyncio.Event()
    calls = []

    async def failing():
        calls.append(1)
        await gate.wait()
        raise RuntimeError("upstream is down")

    waiters = [asyncio.create_task(single_flight.run("boom", failing)) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    outcomes = await asyncio.gather(*waiters, return_exceptions=True)

    assert len(calls) == 1
    assert all(isinstance(o, RuntimeError) for o in outcomes)
    # A rejection is never cached: the key is clean, so the next caller retries.
    assert "boom" not in single_flight._inflight

    gate_2 = asyncio.Event()
    gate_2.set()
    ok_factory, ok_calls = _blocking_factory(gate_2, result="recovered")
    assert await single_flight.run("boom", ok_factory) == "recovered"
    assert len(ok_calls) == 1


async def test_a_cancelled_waiter_does_not_abort_the_shared_fetch():
    gate = asyncio.Event()
    factory, calls = _blocking_factory(gate, result="survived")

    leader = asyncio.create_task(single_flight.run("shared", factory))
    follower = asyncio.create_task(single_flight.run("shared", factory))
    await asyncio.sleep(0)

    # The client that started the fetch walks away. Without the shield in run(),
    # this would cancel the task the follower is waiting on.
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    gate.set()
    assert await follower == "survived"
    assert len(calls) == 1


async def test_fetch_completes_even_when_every_caller_walks_away():
    gate = asyncio.Event()
    finished = []

    async def factory():
        await gate.wait()
        finished.append(1)
        return "done"

    waiters = [asyncio.create_task(single_flight.run("abandoned", factory)) for _ in range(2)]
    await asyncio.sleep(0)
    for w in waiters:
        w.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)

    # The fetch still runs to completion, which is what populates the cache for
    # whoever asks next.
    gate.set()
    await _settle()
    assert finished == [1]
    assert "abandoned" not in single_flight._inflight


# The module is only worth anything if the fetchers actually route through it, so
# this drives the real AniList fetcher with a permanently-cold cache and counts
# the upstream POSTs.
async def test_anilist_metadata_fetcher_coalesces(monkeypatch):
    import metadata_engine.anilist as anilist

    async def _always_miss(_key):
        return None

    async def _no_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(anilist, "get_cached_response", _always_miss)
    monkeypatch.setattr(anilist, "set_cached_response_shadowed", _no_write)

    posts = []
    release = asyncio.Event()

    class CountingClient:
        async def post(self, url, json=None, timeout=None):
            posts.append(json)
            await release.wait()
            return _AniListResponse()

    class _AniListResponse:
        status_code = 200

        def json(self):
            return {"data": {"Media": {"id": 21, "episodes": 2, "title": {"romaji": "One Piece"}}}}

    client = CountingClient()
    waiters = [
        asyncio.create_task(anilist.fetch_anilist_metadata(client, 21)) for _ in range(6)
    ]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*waiters)

    assert len(posts) == 1, "six concurrent misses on one title must be one AniList call"
    assert all(r["anilist_id"] == 21 for r in results)
