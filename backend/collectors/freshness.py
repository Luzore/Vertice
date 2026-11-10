"""Stale-data checks used by collectors (principle 13).

Each function returns a check that receives the payload and the clock time it was
requested for, and answers with a reason in Spanish when the data is too old, or
None when it is fresh.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from core import clock, market_calendar

Freshness = Callable[[Any, datetime], str | None]

# The consolidated tape is delayed 15 minutes on the free plan, so the day's final
# bar is only expected a little after the close.
_DAILY_BAR_GRACE = timedelta(minutes=20)


def expected_latest_session(as_of: datetime) -> date:
    """The most recent session whose daily bar should already exist at ``as_of``."""
    day = clock.market_date(as_of)
    session = market_calendar.get_session(day)
    if session is not None and as_of >= session.close + _DAILY_BAR_GRACE:
        return day
    return market_calendar.previous_trading_day(day)


_FRACTION = re.compile(r"\.(\d+)")


def _parse_instant(value: str) -> datetime:
    """Parse an RFC 3339 instant. Alpaca sends nanoseconds; Python keeps microseconds."""
    text = _FRACTION.sub(lambda match: "." + match.group(1)[:6], value.replace("Z", "+00:00"))
    return clock.ensure_utc(datetime.fromisoformat(text))


def daily_bars(bars_of: Callable[[Any], dict[str, list[dict]]], *, until: date | None) -> Freshness:
    """Stale when no symbol has the bar of the latest completed session.

    Only applies to requests that reach the present: a historical range that ends
    earlier is not stale for ending earlier.
    """

    def check(payload: Any, as_of: datetime) -> str | None:
        expected = expected_latest_session(as_of)
        if until is not None and until < expected:
            return None
        latest: date | None = None
        for bars in bars_of(payload).values():
            if bars:
                bar_day = clock.market_date(_parse_instant(bars[-1]["t"]))
                latest = bar_day if latest is None else max(latest, bar_day)
        if latest is None:
            return f"sin barras diarias; se esperaba la del {expected:%Y-%m-%d}"
        if latest < expected:
            return (
                f"la última barra diaria es del {latest:%Y-%m-%d}; "
                f"se esperaba la del {expected:%Y-%m-%d}"
            )
        return None

    return check


def quotes(quotes_of: Callable[[Any], dict[str, dict]], *, max_age_s: int) -> Freshness:
    """Stale when, with the market open, even the newest quote is too old."""

    def check(payload: Any, as_of: datetime) -> str | None:
        if market_calendar.market_status(as_of) != "open":
            return None
        stamps = [_parse_instant(q["t"]) for q in quotes_of(payload).values() if q.get("t")]
        if not stamps:
            return "sin precios en la respuesta"
        age_s = (as_of - max(stamps)).total_seconds()
        if age_s > max_age_s:
            return f"el precio más reciente tiene {age_s:.0f} s; el máximo es {max_age_s} s"
        return None

    return check


def macro_series(
    latest_date_of: Callable[[Any], date | None], *, max_age_trading_days: int
) -> Freshness:
    """Stale when the newest observation is more than N trading days behind."""

    def check(payload: Any, as_of: datetime) -> str | None:
        latest = latest_date_of(payload)
        if latest is None:
            return "la serie no tiene observaciones"
        today = clock.market_date(as_of)
        behind = len(market_calendar.trading_days_between(latest + timedelta(days=1), today))
        if behind > max_age_trading_days:
            return (
                f"el último dato es del {latest:%Y-%m-%d}, {behind} días hábiles atrás; "
                f"el máximo es {max_age_trading_days}"
            )
        return None

    return check
