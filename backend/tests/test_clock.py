from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import clock

NEW_YORK = ZoneInfo("America/New_York")
MONTERREY = ZoneInfo("America/Monterrey")

WINTER_DAY = date(2026, 12, 15)  # New York on standard time (UTC-5)
SUMMER_DAY = date(2027, 6, 15)  # New York on daylight time (UTC-4)

# ROADMAP section 16: New York time, Monterrey Nov-Mar, Monterrey Mar-Nov.
SCHEDULE = [
    (time(6, 0), time(5, 0), time(4, 0)),
    (time(7, 30), time(6, 30), time(5, 30)),
    (time(8, 30), time(7, 30), time(6, 30)),
    (time(8, 45), time(7, 45), time(6, 45)),
    (time(9, 0), time(8, 0), time(7, 0)),
    (time(9, 30), time(8, 30), time(7, 30)),
    (time(9, 45), time(8, 45), time(7, 45)),
    (time(15, 30), time(14, 30), time(13, 30)),
    (time(16, 15), time(15, 15), time(14, 15)),
    (time(17, 0), time(16, 0), time(15, 0)),
]


# --- New York <-> Monterrey ---------------------------------------------------


def test_plan_time_in_winter():
    published = clock.market_time(WINTER_DAY, time(9, 0))

    assert published == datetime(2026, 12, 15, 14, 0, tzinfo=UTC)
    assert clock.to_user(published).time() == time(8, 0)


def test_plan_time_in_summer():
    published = clock.market_time(SUMMER_DAY, time(9, 0))

    assert published == datetime(2027, 6, 15, 13, 0, tzinfo=UTC)
    assert clock.to_user(published).time() == time(7, 0)


@pytest.mark.parametrize(("new_york", "monterrey_winter", "monterrey_summer"), SCHEDULE)
def test_daily_schedule_matches_the_roadmap(new_york, monterrey_winter, monterrey_summer):
    assert clock.to_user(clock.market_time(WINTER_DAY, new_york)).time() == monterrey_winter
    assert clock.to_user(clock.market_time(SUMMER_DAY, new_york)).time() == monterrey_summer


@pytest.mark.parametrize(
    ("day", "hours_behind"),
    [
        (date(2026, 11, 10), 1),  # after the 2026 change back to standard time
        (date(2027, 3, 12), 1),  # last weekday before daylight time starts (14 Mar 2027)
        (date(2027, 3, 15), 2),  # first weekday after
        (date(2027, 11, 5), 2),  # last weekday before daylight time ends (7 Nov 2027)
        (date(2027, 11, 8), 1),  # first weekday after
    ],
)
def test_gap_changes_on_the_daylight_saving_dates(day, hours_behind):
    opening = clock.market_time(day, time(9, 30))

    new_york_wall = clock.to_market(opening).replace(tzinfo=None)
    monterrey_wall = clock.to_user(opening).replace(tzinfo=None)

    assert new_york_wall - monterrey_wall == timedelta(hours=hours_behind)


def test_monterrey_never_changes_its_clocks():
    offsets = {
        datetime(2027, month, 15, 12, 0, tzinfo=UTC).astimezone(MONTERREY).utcoffset()
        for month in range(1, 13)
    }

    assert offsets == {timedelta(hours=-6)}


def test_monterrey_to_new_york_round_trip():
    in_monterrey = datetime(2027, 6, 15, 7, 0, tzinfo=MONTERREY)

    assert clock.to_market(in_monterrey) == datetime(2027, 6, 15, 9, 0, tzinfo=NEW_YORK)
    assert clock.to_market(in_monterrey).time() == time(9, 0)
    assert clock.to_user(clock.to_market(in_monterrey)).time() == time(7, 0)


def test_market_date_follows_new_york_not_utc():
    late_evening_new_york = datetime(2026, 12, 16, 2, 0, tzinfo=UTC)  # 21:00 on the 15th

    assert clock.market_date(late_evening_new_york) == date(2026, 12, 15)


def test_user_time_zone_can_be_changed():
    published = clock.market_time(WINTER_DAY, time(9, 0))

    assert clock.to_user(published, ZoneInfo("Europe/Madrid")).time() == time(15, 0)


def test_dual_label():
    published = clock.market_time(SUMMER_DAY, time(9, 0))

    assert clock.dual_label(published) == "09:00 NY · 07:00 Monterrey"


# --- naive datetimes are bugs ----------------------------------------------------


@pytest.mark.parametrize("function", [clock.ensure_utc, clock.to_market, clock.to_user])
def test_naive_datetime_is_refused(function):
    with pytest.raises(ValueError, match="naive"):
        function(datetime(2026, 12, 15, 9, 0))  # noqa: DTZ001 - naive on purpose


def test_market_time_refuses_a_time_with_a_zone():
    with pytest.raises(ValueError, match="wall-clock"):
        clock.market_time(WINTER_DAY, time(9, 0, tzinfo=UTC))


def test_ensure_utc_converts_other_zones():
    assert clock.ensure_utc(datetime(2026, 12, 15, 9, 0, tzinfo=NEW_YORK)) == datetime(
        2026, 12, 15, 14, 0, tzinfo=UTC
    )


# --- real and simulated clock -----------------------------------------------------


def test_real_clock_is_utc_and_current():
    before = datetime.now(UTC)
    moment = clock.now()
    after = datetime.now(UTC)

    assert moment.tzinfo is UTC
    assert before <= moment <= after
    assert clock.is_simulated() is False


def test_simulated_clock_stands_still_until_moved():
    start = clock.market_time(date(2026, 5, 4), time(7, 30))

    with clock.simulated(start) as sim:
        assert clock.is_simulated() is True
        assert clock.now() == start
        assert clock.now() == start  # no drift between calls

        sim.advance(timedelta(minutes=75))
        assert clock.now() == clock.market_time(date(2026, 5, 4), time(8, 45))

        sim.set(clock.market_time(date(2026, 5, 5), time(9, 0)))
        assert clock.market_date() == date(2026, 5, 5)


def test_real_clock_returns_after_the_simulation():
    with clock.simulated(datetime(2020, 1, 2, 15, 0, tzinfo=UTC)):
        assert clock.now().year == 2020

    assert clock.is_simulated() is False
    assert clock.now().year >= 2026


def test_real_clock_returns_even_if_the_block_fails():
    with pytest.raises(RuntimeError), clock.simulated(datetime(2020, 1, 2, tzinfo=UTC)):
        raise RuntimeError("replay crashed")

    assert clock.is_simulated() is False


def test_simulated_clock_cannot_advance_backwards():
    sim = clock.SimulatedClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))

    with pytest.raises(ValueError, match="forward"):
        sim.advance(timedelta(seconds=-1))


def test_simulated_clock_refuses_naive_start():
    with pytest.raises(ValueError, match="naive"):
        clock.SimulatedClock(datetime(2026, 5, 4, 12, 0))  # noqa: DTZ001


def test_simulated_clock_stores_utc():
    sim = clock.SimulatedClock(datetime(2026, 12, 15, 9, 0, tzinfo=NEW_YORK))

    assert sim.now() == datetime(2026, 12, 15, 14, 0, tzinfo=UTC)
    assert sim.now().tzinfo is UTC


def test_replay_in_one_task_does_not_change_time_for_another():
    """A replay and the live scheduler can run side by side in the same process."""
    replay_time = datetime(2026, 5, 4, 13, 30, tzinfo=UTC)
    seen: dict[str, datetime] = {}

    async def replay():
        with clock.simulated(replay_time):
            await asyncio.sleep(0.01)
            seen["replay"] = clock.now()

    async def live():
        await asyncio.sleep(0.005)  # runs while the replay task holds its simulated clock
        seen["live"] = clock.now()

    async def main():
        await asyncio.gather(replay(), live())

    asyncio.run(main())

    assert seen["replay"] == replay_time
    assert seen["live"].year >= 2026
