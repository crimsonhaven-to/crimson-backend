import time

from core.bounded_cache import BoundedCache


def test_the_oldest_entry_goes_first_when_full():
    cache = BoundedCache(2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert (cache.get("b"), cache.get("c")) == (2, 3)


def test_an_expired_entry_is_a_miss(monkeypatch):
    cache = BoundedCache(2)
    cache.set("a", 1, ttl=10)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 11)
    assert cache.get("a") is None


def test_falsy_values_are_hits():
    cache = BoundedCache(2)
    cache.set("empty", [])
    assert cache.get("empty") == []
