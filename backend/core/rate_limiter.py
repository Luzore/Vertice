"""Rate limiting for data sources (principle 13).

Each source gets its own token bucket sized so that the provider's published limit
can never be exceeded, even right after an idle period:

* tokens refill at ``utilization`` (80%) of the provider's rate;
* the bucket holds at most the remaining 20% as a burst.

In any window as long as the provider's own, the calls made are at most
``burst + refill`` = 20% + 80% of the limit.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable

_EPSILON = 1e-9
_MIN_WAIT_S = 1e-6


class TokenBucket:
    def __init__(
        self,
        *,
        rate_per_s: float,
        capacity: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_s <= 0 or capacity < 1:
            raise ValueError("rate_per_s must be positive and capacity at least 1")
        self._rate = rate_per_s
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._monotonic = monotonic
        self._sleep = sleep
        self._updated = monotonic()
        self._paused_until = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def for_limit(
        cls,
        calls: int,
        per_seconds: float,
        *,
        utilization: float = 0.8,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> TokenBucket:
        """A bucket that stays within a provider limit of ``calls`` per ``per_seconds``."""
        if not 0 < utilization <= 1:
            raise ValueError("utilization must be in (0, 1]")
        return cls(
            rate_per_s=calls * utilization / per_seconds,
            # Rounded first: 60 * (1 - 0.8) is 11.999999999999998 in floating point.
            capacity=max(1, math.floor(round(calls * (1 - utilization), 9))),
            monotonic=monotonic,
            sleep=sleep,
        )

    def _refill(self, now: float) -> None:
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def acquire(self) -> float:
        """Wait until one call is allowed. Returns the seconds spent waiting."""
        waited = 0.0
        # Waiters queue on the lock, so calls leave in the order they arrived.
        async with self._lock:
            while True:
                now = self._monotonic()
                if now < self._paused_until:
                    delay = self._paused_until - now
                else:
                    self._refill(now)
                    # The tolerance matters: refilling in floating point can leave
                    # 0.9999999999999999 tokens, and waiting for the missing sliver
                    # is a wait too short to move any clock, i.e. an endless loop.
                    if self._tokens >= 1 - _EPSILON:
                        self._tokens = max(0.0, self._tokens - 1)
                        return waited
                    delay = max((1 - self._tokens) / self._rate, _MIN_WAIT_S)
                await self._sleep(delay)
                waited += delay

    def pause_for(self, seconds: float) -> None:
        """Block every caller for ``seconds``: the provider said to back off (HTTP 429)."""
        if seconds > 0:
            self._paused_until = max(self._paused_until, self._monotonic() + seconds)
