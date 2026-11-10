"""NYSE trading calendar: trading days, holidays and early closes.

Nasdaq keeps the same schedule, so one calendar (XNYS) serves the whole universe.
Session times are returned in UTC; convert with ``core.clock`` for display.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from typing import Literal

import exchange_calendars as xcals
import pandas as pd

from core import clock

MarketStatus = Literal["open", "premarket", "closed"]

# US pre-market trading starts at 04:00 New York time.
PREMARKET_START = time(4, 0)


class MarketCalendarError(Exception):
    """The date is outside the range the calendar knows about."""


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime
    close: datetime
    early_close: bool


@lru_cache(maxsize=1)
def _nyse() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS")


def _check_bounds(day: date) -> None:
    calendar = _nyse()
    first, last = calendar.first_session.date(), calendar.last_session.date()
    if not first <= day <= last:
        raise MarketCalendarError(f"{day} is outside the NYSE calendar range ({first} to {last})")


def _to_date(stamp: pd.Timestamp) -> date:
    return stamp.date()


def is_trading_day(day: date) -> bool:
    _check_bounds(day)
    return bool(_nyse().is_session(pd.Timestamp(day)))


def get_session(day: date) -> Session | None:
    """Open and close of ``day`` in UTC, or None when the market is closed all day."""
    if not is_trading_day(day):
        return None
    calendar = _nyse()
    stamp = pd.Timestamp(day)
    return Session(
        day=day,
        open=calendar.session_open(stamp).to_pydatetime(),
        close=calendar.session_close(stamp).to_pydatetime(),
        early_close=stamp in calendar.early_closes,
    )


def next_trading_day(day: date) -> date:
    """First trading day strictly after ``day``."""
    return add_trading_days(day, 1)


def previous_trading_day(day: date) -> date:
    """Last trading day strictly before ``day``."""
    return add_trading_days(day, -1)


def add_trading_days(day: date, count: int) -> date:
    """Move ``count`` trading days forward (or back when negative) from ``day``.

    ``day`` itself does not need to be a trading day: from a Saturday, +1 is Monday
    and -1 is Friday.
    """
    _check_bounds(day)
    if count == 0:
        raise ValueError("count must not be zero; use is_trading_day() to test a date")
    calendar = _nyse()
    stamp = pd.Timestamp(day)
    if count > 0:
        anchor = calendar.date_to_session(stamp, direction="previous")
    else:
        anchor = calendar.date_to_session(stamp, direction="next")
    try:
        return _to_date(calendar.session_offset(anchor, count))
    except (ValueError, IndexError) as exc:
        raise MarketCalendarError(
            f"{count:+d} trading days from {day} is outside the NYSE calendar range"
        ) from exc


def trading_days_between(start: date, end: date) -> list[date]:
    """Trading days from ``start`` to ``end``, both included."""
    if end < start:
        return []
    _check_bounds(start)
    _check_bounds(end)
    sessions = _nyse().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [_to_date(stamp) for stamp in sessions]


def market_status(moment: datetime | None = None) -> MarketStatus:
    """Whether the market is open, in pre-market, or closed at ``moment`` (default: now)."""
    moment = clock.ensure_utc(moment) if moment is not None else clock.now()
    day = clock.market_date(moment)
    session = get_session(day)
    if session is None:
        return "closed"
    if session.open <= moment < session.close:
        return "open"
    if clock.market_time(day, PREMARKET_START) <= moment < session.open:
        return "premarket"
    return "closed"
