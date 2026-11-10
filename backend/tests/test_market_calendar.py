from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from core import clock, market_calendar
from core.market_calendar import MarketCalendarError


def test_regular_session_in_winter():
    session = market_calendar.get_session(date(2026, 11, 10))

    assert session.open == datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
    assert session.close == datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
    assert session.early_close is False
    assert clock.to_market(session.open).time() == time(9, 30)
    assert clock.to_market(session.close).time() == time(16, 0)


def test_regular_session_in_summer():
    session = market_calendar.get_session(date(2027, 6, 15))

    assert session.open == datetime(2027, 6, 15, 13, 30, tzinfo=UTC)
    assert session.close == datetime(2027, 6, 15, 20, 0, tzinfo=UTC)
    assert clock.to_market(session.open).time() == time(9, 30)


@pytest.mark.parametrize(
    ("day", "why"),
    [
        (date(2026, 11, 14), "Saturday"),
        (date(2026, 11, 15), "Sunday"),
        (date(2026, 11, 26), "Thanksgiving"),
        (date(2026, 12, 25), "Christmas"),
        (date(2027, 1, 1), "New Year's Day"),
        (date(2027, 1, 18), "Martin Luther King Jr. Day"),
        (date(2027, 3, 26), "Good Friday"),
        (date(2027, 6, 18), "Juneteenth, observed on Friday"),
        (date(2027, 7, 5), "Independence Day, observed on Monday"),
    ],
)
def test_market_is_closed(day, why):
    assert market_calendar.is_trading_day(day) is False, why
    assert market_calendar.get_session(day) is None


@pytest.mark.parametrize("day", [date(2026, 11, 27), date(2026, 12, 24)])
def test_early_close_is_at_1pm_new_york(day):
    session = market_calendar.get_session(day)

    assert session.early_close is True
    assert clock.to_market(session.close).time() == time(13, 0)
    assert clock.to_market(session.open).time() == time(9, 30)


def test_next_and_previous_skip_holidays_and_weekends():
    day_before_thanksgiving = date(2026, 11, 25)

    assert market_calendar.next_trading_day(day_before_thanksgiving) == date(2026, 11, 27)
    assert market_calendar.previous_trading_day(date(2026, 11, 27)) == day_before_thanksgiving
    assert market_calendar.next_trading_day(date(2026, 11, 27)) == date(2026, 11, 30)


def test_next_and_previous_from_a_closed_day():
    saturday = date(2026, 11, 14)

    assert market_calendar.next_trading_day(saturday) == date(2026, 11, 16)
    assert market_calendar.previous_trading_day(saturday) == date(2026, 11, 13)


def test_add_trading_days_counts_only_sessions():
    # Earnings block: 3 trading days from the Wednesday before Thanksgiving.
    assert market_calendar.add_trading_days(date(2026, 11, 25), 3) == date(2026, 12, 1)
    # Cooldown after a stop: 5 trading days back from Tuesday 1 December.
    assert market_calendar.add_trading_days(date(2026, 12, 1), -5) == date(2026, 11, 23)


def test_add_trading_days_refuses_zero():
    with pytest.raises(ValueError, match="zero"):
        market_calendar.add_trading_days(date(2026, 11, 25), 0)


def test_trading_days_between_is_inclusive():
    thanksgiving_week = market_calendar.trading_days_between(date(2026, 11, 23), date(2026, 11, 27))

    assert thanksgiving_week == [
        date(2026, 11, 23),
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
    ]
    assert market_calendar.trading_days_between(date(2026, 11, 27), date(2026, 11, 23)) == []
    assert market_calendar.trading_days_between(date(2026, 11, 14), date(2026, 11, 15)) == []


@pytest.mark.parametrize(
    ("new_york_time", "expected"),
    [
        (time(3, 59), "closed"),
        (time(4, 0), "premarket"),
        (time(9, 0), "premarket"),
        (time(9, 29), "premarket"),
        (time(9, 30), "open"),
        (time(15, 59), "open"),
        (time(16, 0), "closed"),
        (time(17, 0), "closed"),
    ],
)
def test_market_status_through_a_regular_day(new_york_time, expected):
    moment = clock.market_time(date(2026, 11, 10), new_york_time)

    assert market_calendar.market_status(moment) == expected


def test_market_status_on_an_early_close_day():
    day = date(2026, 11, 27)

    assert market_calendar.market_status(clock.market_time(day, time(12, 59))) == "open"
    assert market_calendar.market_status(clock.market_time(day, time(13, 0))) == "closed"


@pytest.mark.parametrize("day", [date(2026, 11, 14), date(2026, 11, 26)])
def test_market_status_is_closed_all_day_without_a_session(day):
    assert market_calendar.market_status(clock.market_time(day, time(10, 0))) == "closed"


def test_market_status_uses_the_active_clock():
    with clock.simulated(clock.market_time(date(2026, 5, 4), time(10, 0))) as sim:
        assert market_calendar.market_status() == "open"

        sim.set(clock.market_time(date(2026, 5, 2), time(10, 0)))  # a Saturday
        assert market_calendar.market_status() == "closed"


def test_dates_outside_the_calendar_are_reported():
    with pytest.raises(MarketCalendarError, match="outside the NYSE calendar range"):
        market_calendar.is_trading_day(date(2090, 1, 2))
    with pytest.raises(MarketCalendarError):
        market_calendar.trading_days_between(date(1990, 1, 2), date(1990, 1, 5))
