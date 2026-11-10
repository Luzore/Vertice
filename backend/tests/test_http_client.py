from __future__ import annotations

import asyncio

import httpx2 as httpx
import pytest

from collectors.http import SourceClient, SourceError
from tests.conftest import max_calls_in_any_window

pytestmark = pytest.mark.anyio

URL = "https://finnhub.io/api/v1/quote"


@pytest.fixture
def make_client(data_config, fake_time):
    def _make(handler, source: str = "finnhub") -> SourceClient:
        return SourceClient(
            source,
            data_config.sources[source],
            data_config,
            transport=httpx.MockTransport(handler),
            sleep=fake_time.sleep,
            monotonic=fake_time.monotonic,
            jitter=lambda: 0.0,
        )

    return _make


# --- acceptance criterion: HTTP 429 -------------------------------------------------


async def test_429_waits_what_the_provider_asks_then_retries(make_client, fake_time):
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(fake_time.now)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"c": 752.69})

    response, _ = await make_client(handler).get(URL, params={"symbol": "SPY"})

    assert response.json() == {"c": 752.69}
    assert len(seen) == 2
    assert seen[1] - seen[0] >= 7


async def test_429_without_retry_after_backs_off_exponentially(make_client, fake_time):
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(fake_time.now)
        return httpx.Response(429) if len(seen) <= 3 else httpx.Response(200, json={})

    await make_client(handler).get(URL)

    gaps = [later - earlier for earlier, later in zip(seen, seen[1:], strict=False)]
    assert gaps == pytest.approx([1, 2, 4])


async def test_429_makes_every_caller_of_that_source_wait(make_client, fake_time):
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(fake_time.now)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "20"})
        return httpx.Response(200, json={})

    client = make_client(handler)
    await asyncio.gather(*(client.get(URL) for _ in range(5)))

    assert len(seen) == 6  # 5 requests plus the one retry
    assert all(moment - seen[0] >= 20 for moment in seen[1:])


@pytest.mark.parametrize("source", ["finnhub", "alpaca", "fred", "massive"])
async def test_retries_after_429_never_exceed_the_provider_limit(
    make_client, fake_time, data_config, source
):
    """A provider that enforces its limit exactly never has to reject us twice."""
    limit = data_config.sources[source].limit_calls
    period = data_config.sources[source].limit_period_s
    seen: list[float] = []
    rejected = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal rejected
        seen.append(fake_time.now)
        # The provider throws one 429 early on, for a reason of its own.
        if len(seen) == 3:
            rejected += 1
            return httpx.Response(429, headers={"Retry-After": "2"})
        # From then on it rejects only if its published limit is really exceeded.
        if max_calls_in_any_window(seen, period) > limit:
            rejected += 1
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={})

    client = make_client(handler, source)
    results = await asyncio.gather(*(client.get(URL) for _ in range(limit * 3)))

    assert len(results) == limit * 3
    assert rejected == 1
    assert max_calls_in_any_window(seen, period) <= limit


async def test_retry_after_longer_than_the_cap_is_capped(make_client, fake_time, data_config):
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(fake_time.now)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "86400"})
        return httpx.Response(200, json={})

    await make_client(handler).get(URL)

    assert seen[1] - seen[0] == pytest.approx(data_config.backoff_max_s)


async def test_retry_after_as_http_date_is_understood(make_client, fake_time):
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(fake_time.now)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        return httpx.Response(200, json={})

    await make_client(handler).get(URL)

    assert seen[1] - seen[0] == 0  # a date in the past means "retry now"


# --- other failures ---------------------------------------------------------------


async def test_gives_up_after_the_configured_retries(make_client, data_config):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    with pytest.raises(SourceError, match=r"HTTP 503 \(error del servidor\), tras 5 intentos"):
        await make_client(handler).get(URL)

    assert attempts == data_config.max_retries + 1


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "credenciales rechazadas"), (403, "acceso denegado"), (404, "no encontrado")],
)
async def test_client_errors_are_not_retried(make_client, status, reason):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status)

    with pytest.raises(SourceError, match=reason) as error:
        await make_client(handler).get(URL)

    assert attempts == 1
    assert error.value.status_code == status


async def test_network_failures_are_retried(make_client):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection refused")
        if attempts == 2:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"ok": True})

    response, _ = await make_client(handler).get(URL)

    assert attempts == 3
    assert response.json() == {"ok": True}


async def test_timeout_message_after_all_retries(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(SourceError, match="finnhub.io no respondió en 15 s, tras 5 intentos"):
        await make_client(handler).get(URL)


async def test_error_messages_never_contain_the_url_or_its_secrets(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(SourceError) as error:
        await make_client(handler, "fred").get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "VIXCLS", "api_key": "0123456789abcdef0123456789abcdef"},
        )

    message = str(error.value)
    assert message == "api.stlouisfed.org respondió HTTP 403 (acceso denegado)"
    assert "0123456789abcdef" not in message


async def test_latency_is_measured_per_request(make_client, fake_time):
    def handler(request: httpx.Request) -> httpx.Response:
        fake_time.now += 0.182  # the request takes 182 ms
        return httpx.Response(200, json={})

    _, latency_ms = await make_client(handler).get(URL)

    assert latency_ms == 182


async def test_requests_carry_params_headers_and_a_user_agent(make_client):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    await make_client(handler).get(URL, params={"symbol": "SPY"}, headers={"X-Finnhub-Token": "k"})

    (request,) = captured
    assert request.url.params["symbol"] == "SPY"
    assert request.headers["X-Finnhub-Token"] == "k"
    assert request.headers["User-Agent"].startswith("VERTICE/")
