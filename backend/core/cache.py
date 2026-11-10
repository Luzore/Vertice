"""In-memory cache with a time-to-live per entry.

Used by collectors so the same question is not asked twice within a short time
(for example, at most one RSS query per ticker every 15 minutes).
"""

from __future__ import annotations

import time
from collections.abc import Callable


class TTLCache[T]:
    def __init__(
        self, *, monotonic: Callable[[], float] = time.monotonic, max_entries: int = 4096
    ) -> None:
        self._monotonic = monotonic
        self._max_entries = max_entries
        self._entries: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._monotonic() >= expires_at:
            del self._entries[key]
            return None
        return value

    def set(self, key: str, value: T, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        if len(self._entries) >= self._max_entries:
            self._evict()
        self._entries[key] = (self._monotonic() + ttl_s, value)

    def _evict(self) -> None:
        now = self._monotonic()
        expired = [key for key, (expires_at, _) in self._entries.items() if expires_at <= now]
        for key in expired:
            del self._entries[key]
        # Still full: drop the entries closest to expiring.
        overflow = len(self._entries) - self._max_entries + 1
        if overflow > 0:
            for key, _ in sorted(self._entries.items(), key=lambda item: item[1][0])[:overflow]:
                del self._entries[key]

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
