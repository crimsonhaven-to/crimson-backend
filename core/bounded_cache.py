"""An in-process dict with a size ceiling and optional per-entry expiry."""

import time
from typing import Any, Hashable, Optional


class BoundedCache:
    def __init__(self, max_entries: int) -> None:
        self._max = max_entries
        self._data: dict[Hashable, tuple[Optional[float], Any]] = {}

    def get(self, key: Hashable) -> Any:
        """The stored value, or None when absent or expired."""
        hit = self._data.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if expires_at is not None and expires_at <= time.monotonic():
            del self._data[key]
            return None
        return value

    def set(self, key: Hashable, value: Any, ttl: Optional[float] = None) -> None:
        self._data.pop(key, None)
        if len(self._data) >= self._max:
            now = time.monotonic()
            for stale in [k for k, (exp, _) in self._data.items() if exp is not None and exp <= now]:
                del self._data[stale]
        while len(self._data) >= self._max:
            # Dicts keep insertion order, so this drops the oldest entry.
            del self._data[next(iter(self._data))]
        self._data[key] = (None if ttl is None else time.monotonic() + ttl, value)

    def pop(self, key: Hashable) -> None:
        self._data.pop(key, None)
