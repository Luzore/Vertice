from __future__ import annotations

import asyncio

import pytest

from core.rate_limiter import TokenBucket
from tests.conftest import max_calls_in_any_window

pytestmark = pytest.mark.anyio

# (provider limit, period in seconds) of every source in ROADMAP section 5.
PROVIDER_LIMITS = [
    pytest.param(200, 60, id="alpaca-200-per-minute"),
    pytest.param(60, 60, id="finnhub-60-per-minute"),
    pytest.param(120, 60, id="fred-120-per-minute"),
    pytest.param(10, 1, id="sec-10-per-second"),
    pytest.param(5, 60, id="massive-5-per-minute"),
    pytest.param(1, 60, id="one-per-minute"),
]


async def _run(bucket: TokenBucket, fake_time, count: int) -> list[float]:
    times = []
    for _ in range(count):
        await bucket.acquire()
        times.append(fake_time.now)
    return times


@pytest.mark.parametrize(("limit", "period"), PROVIDER_LIMITS)
async def test_never_exceeds_the_provider_limit_in_any_window(fake_time, limit, period):
    bucket = TokenBucket.for_limit(
        limit, period, monotonic=fake_time.monotonic, sleep=fake_time.sleep
    )

    times = await _run(bucket, fake_time, limit * 6)

    assert max_calls_in_any_window(times, period) <= limit


@pytest.mark.parametrize(("limit", "period"), PROVIDER_LIMITS)
async def test_sustained_rate_is_80_percent_of_the_limit(fake_time, limit, period):
    bucket = TokenBucket.for_limit(
        limit, period, monotonic=fake_time.monotonic, sleep=fake_time.sleep
    )
    count = limit * 20

    times = await _run(bucket, fake_time, count)

    achieved_per_period = count / (times[-1] - times[0]) * period
    assert achieved_per_period == pytest.approx(limit * 0.8, rel=0.06)


async def test_burst_after_idle_time_is_capped(fake_time):
    bucket = TokenBucket.for_limit(60, 60, monotonic=fake_time.monotonic, sleep=fake_time.sleep)
    fake_time.now += 3600  # an hour without calls must not bank an hour of tokens

    times = await _run(bucket, fake_time, 120)

    assert max_calls_in_any_window(times, 60) <= 60
    immediate = [t for t in times if t == times[0]]
    assert len(immediate) == 12  # the burst: 20% of the limit


async def test_first_calls_do_not_wait_and_later_ones_do(fake_time):
    bucket = TokenBucket.for_limit(60, 60, monotonic=fake_time.monotonic, sleep=fake_time.sleep)

    waits = [await bucket.acquire() for _ in range(13)]

    assert waits[:12] == [0.0] * 12
    assert waits[12] == pytest.approx(1.25)  # one token at 48 per minute


async def test_pause_blocks_every_caller(fake_time):
    bucket = TokenBucket.for_limit(200, 60, monotonic=fake_time.monotonic, sleep=fake_time.sleep)
    start = fake_time.now

    bucket.pause_for(30)
    await bucket.acquire()
    await bucket.acquire()

    assert fake_time.now - start == pytest.approx(30)


async def test_shorter_pause_does_not_cut_a_longer_one(fake_time):
    bucket = TokenBucket.for_limit(200, 60, monotonic=fake_time.monotonic, sleep=fake_time.sleep)
    start = fake_time.now

    bucket.pause_for(30)
    bucket.pause_for(5)
    await bucket.acquire()

    assert fake_time.now - start == pytest.approx(30)


async def test_concurrent_callers_share_the_limit(fake_time):
    bucket = TokenBucket.for_limit(60, 60, monotonic=fake_time.monotonic, sleep=fake_time.sleep)
    times: list[float] = []

    async def worker():
        await bucket.acquire()
        times.append(fake_time.now)

    await asyncio.gather(*(worker() for _ in range(300)))

    assert len(times) == 300
    assert max_calls_in_any_window(times, 60) <= 60


@pytest.mark.parametrize("utilization", [0, 1.5, -0.2])
async def test_invalid_utilization_is_refused(utilization):
    with pytest.raises(ValueError, match="utilization"):
        TokenBucket.for_limit(60, 60, utilization=utilization)


async def test_a_sliver_short_of_one_token_does_not_stall(fake_time):
    """Regression: floating point can leave 0.9999999999999999 tokens. Waiting for
    the missing sliver is too short to move a clock, so acquire() looped forever."""
    bucket = TokenBucket.for_limit(60, 60, monotonic=fake_time.monotonic, sleep=fake_time.sleep)
    bucket._tokens = 1 - 1e-13

    waited = await bucket.acquire()

    assert waited == 0.0
    assert fake_time.sleeps == []
