from __future__ import annotations

from datetime import UTC, date, datetime

import httpx2 as httpx
import pytest

from collectors.base import Collector, SnapshotStatus, request_key
from collectors.http import SourceClient
from core import clock

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


class DemoCollector(Collector):
    source = "finnhub"

    async def quote(self, symbol: str, *, as_of=None, freshness=None):
        async def fetch(requests):
            return await requests.get_json(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": symbol, "token": self._secret("finnhub_api_key")},
            )

        return await self._collect(
            "company_news", request_key(symbol=symbol), fetch, as_of=as_of, freshness=freshness
        )


def test_request_key_is_canonical():
    key = request_key(symbols=["SPY", "aapl".upper()], start=date(2026, 11, 2), end=None)

    assert key == "start=2026-11-02&symbols=AAPL,SPY"
    assert key == request_key(start=date(2026, 11, 2), symbols=("AAPL", "SPY"))
    assert request_key(at=NOW) == "at=2026-11-10T15:00:00Z"


async def test_successful_fetch(make_runtime, recorder, fake_time):
    def handler(request: httpx.Request) -> httpx.Response:
        fake_time.now += 0.25
        return httpx.Response(200, json={"c": 752.69})

    snapshot = await DemoCollector(make_runtime(handler)).quote("SPY", as_of=NOW)

    assert snapshot.status is SnapshotStatus.OK
    assert snapshot.usable
    assert snapshot.payload == {"c": 752.69}
    assert snapshot.latency_ms == 250
    assert snapshot.as_of == NOW
    assert (snapshot.source, snapshot.dataset, snapshot.request_key) == (
        "finnhub",
        "company_news",
        "symbol=SPY",
    )
    assert snapshot.message is None
    assert recorder.snapshots == [snapshot]


async def test_as_of_defaults_to_the_active_clock(make_runtime):
    runtime = make_runtime(lambda request: httpx.Response(200, json={}))
    replay_time = datetime(2026, 5, 4, 13, 30, tzinfo=UTC)

    with clock.simulated(replay_time):
        snapshot = await DemoCollector(runtime).quote("SPY")

    assert snapshot.as_of == replay_time
    assert snapshot.fetched_at.year >= 2026  # real time, not the simulated one
    assert snapshot.fetched_at != replay_time


async def test_source_without_credentials_is_not_contacted(make_runtime, no_secrets, recorder):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    collector = DemoCollector(make_runtime(handler, secrets=no_secrets))
    snapshot = await collector.quote("SPY", as_of=NOW)

    assert collector.configured is False
    assert snapshot.status is SnapshotStatus.NOT_CONFIGURED
    assert snapshot.message == "sin configurar: falta FINNHUB_API_KEY"
    assert snapshot.usable is False
    assert calls == 0
    assert recorder.snapshots == []


async def test_repeated_request_is_served_from_cache(make_runtime, recorder, fake_time):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"call": calls})

    collector = DemoCollector(make_runtime(handler))
    first = await collector.quote("SPY", as_of=NOW)
    second = await collector.quote("SPY", as_of=NOW)
    other = await collector.quote("AAPL", as_of=NOW)

    assert calls == 2  # SPY once, AAPL once
    assert second.from_cache is True
    assert second.payload == first.payload == {"call": 1}
    assert other.from_cache is False
    assert len(recorder.snapshots) == 2  # cache hits are not recorded again

    fake_time.now += 301  # company_news is cached for 300 s in config.yaml
    third = await collector.quote("SPY", as_of=NOW)
    assert third.from_cache is False
    assert third.payload == {"call": 3}


async def test_failure_becomes_an_error_snapshot_without_secrets(make_runtime, recorder):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    collector = DemoCollector(make_runtime(handler))
    snapshot = await collector.quote("SPY", as_of=NOW)

    assert snapshot.status is SnapshotStatus.ERROR
    assert snapshot.payload is None
    assert snapshot.usable is False
    assert snapshot.message == "finnhub.io respondió HTTP 403 (acceso denegado)"
    assert "test-finnhub-key" not in snapshot.message
    assert recorder.snapshots == [snapshot]

    # Errors are not cached: the next call tries again.
    again = await collector.quote("SPY", as_of=NOW)
    assert again.from_cache is False


async def test_slow_response_is_flagged(make_runtime, fake_time):
    def handler(request: httpx.Request) -> httpx.Response:
        fake_time.now += 3.5
        return httpx.Response(200, json={})

    snapshot = await DemoCollector(make_runtime(handler)).quote("SPY", as_of=NOW)

    assert snapshot.status is SnapshotStatus.SLOW
    assert snapshot.message == "respuesta lenta: 3500 ms"
    assert snapshot.usable


async def test_stale_data_is_flagged_and_not_cached(make_runtime):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"date": "2026-10-01"})

    def too_old(payload, as_of):
        return f"el último dato es del {payload['date']}"

    collector = DemoCollector(make_runtime(handler))
    snapshot = await collector.quote("SPY", as_of=NOW, freshness=too_old)
    await collector.quote("SPY", as_of=NOW, freshness=too_old)

    assert snapshot.status is SnapshotStatus.STALE
    assert snapshot.message == "el último dato es del 2026-10-01"
    assert snapshot.usable  # old data is still data; the status carries the warning
    assert calls == 2


async def test_non_json_response_is_an_error(make_runtime):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    snapshot = await DemoCollector(make_runtime(handler)).quote("SPY", as_of=NOW)

    assert snapshot.status is SnapshotStatus.ERROR
    assert snapshot.message == "finnhub.io devolvió una respuesta que no es JSON"


async def test_collectors_of_one_source_share_one_rate_limiter(make_runtime):
    runtime = make_runtime(lambda request: httpx.Response(200, json={}))

    assert DemoCollector(runtime).http is DemoCollector(runtime).http
    assert runtime.client("finnhub") is not runtime.client("fred")


def test_the_http_layer_can_only_read():
    """Principle 1: there is no way to send an order, or anything else, to a provider."""
    writers = {"post", "put", "patch", "delete", "request", "send"}

    assert writers.isdisjoint(dir(SourceClient))
