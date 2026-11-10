"""The system clock.

Every part of the pipeline asks this module for the time instead of the operating
system. That is what makes replay possible (principle 15): a simulated clock is put
in place and the same code runs over historical days without seeing the future.

All datetimes handled here are timezone-aware and stored in UTC (principle 11).
A naive datetime is always a bug, so it raises instead of being guessed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
USER_TZ = ZoneInfo("America/Monterrey")


class Clock(Protocol):
    def now(self) -> datetime: ...


def ensure_utc(moment: datetime) -> datetime:
    """Return ``moment`` in UTC, refusing datetimes without a time zone."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed, attach a time zone: {moment!r}")
    return moment.astimezone(UTC)


class SystemClock:
    """Real time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SimulatedClock:
    """A clock that only moves when told to. Used by replay and by tests."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = ensure_utc(moment)

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("advance() only moves forward; use set() to jump back")
        self._now += delta


_SYSTEM_CLOCK = SystemClock()
# A ContextVar so a replay running in one task cannot change the time seen by the
# live scheduler running in another.
_current: ContextVar[Clock] = ContextVar("vertice_clock", default=_SYSTEM_CLOCK)


def now() -> datetime:
    """Current time in UTC according to the active clock."""
    return _current.get().now()


def get_clock() -> Clock:
    return _current.get()


def is_simulated() -> bool:
    return not isinstance(_current.get(), SystemClock)


@contextlib.contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Make ``clock`` the active clock inside the ``with`` block."""
    token = _current.set(clock)
    try:
        yield clock
    finally:
        _current.reset(token)


@contextlib.contextmanager
def simulated(start: datetime) -> Iterator[SimulatedClock]:
    """Run the ``with`` block on a simulated clock that starts at ``start``."""
    clock = SimulatedClock(start)
    with use_clock(clock):
        yield clock


def to_market(moment: datetime) -> datetime:
    """Convert to New York time."""
    return ensure_utc(moment).astimezone(MARKET_TZ)


def to_user(moment: datetime, tz: ZoneInfo = USER_TZ) -> datetime:
    """Convert to the user's time (Monterrey unless config says otherwise)."""
    return ensure_utc(moment).astimezone(tz)


def market_date(moment: datetime | None = None) -> date:
    """The calendar date in New York at ``moment`` (default: now)."""
    return to_market(moment if moment is not None else now()).date()


def market_time(day: date, at: time) -> datetime:
    """The UTC instant of a New York wall-clock time, e.g. 09:00 ET on a given day."""
    if at.tzinfo is not None:
        raise ValueError("pass a plain wall-clock time; it is interpreted as New York time")
    return datetime.combine(day, at, tzinfo=MARKET_TZ).astimezone(UTC)


def dual_label(moment: datetime, tz: ZoneInfo = USER_TZ) -> str:
    """Short label for the UI with both time zones, e.g. '09:00 NY · 08:00 Monterrey'."""
    city = tz.key.split("/")[-1].replace("_", " ")
    return f"{to_market(moment):%H:%M} NY · {to_user(moment, tz):%H:%M} {city}"
