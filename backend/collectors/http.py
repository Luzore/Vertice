"""HTTP access for one data source: rate limit, timeout, retries with backoff.

Every request to a provider goes through a ``SourceClient``. It waits for the
source's rate limiter before each attempt, retries what is worth retrying
(HTTP 429, 5xx, network failures) and gives up with a ``SourceError`` otherwise.

Error messages name the host only, never the path or the query string, because
two providers carry the key there (FRED in the query, Telegram in the path).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2 as httpx

from core.logging import get_logger
from core.rate_limiter import TokenBucket
from core.settings import DataConfig, SourceConfig

log = get_logger(__name__)

DEFAULT_USER_AGENT = "VERTICE/0.1"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class SourceError(Exception):
    """A source could not deliver. The message is in Spanish: it is shown in the UI."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Seconds the provider asked us to wait, from the Retry-After header."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class SourceClient:
    def __init__(
        self,
        source: str,
        source_config: SourceConfig,
        data_config: DataConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.source = source
        self._timeout_s = source_config.timeout_s
        self._max_retries = (
            source_config.max_retries
            if source_config.max_retries is not None
            else data_config.max_retries
        )
        self._backoff_base_s = data_config.backoff_base_s
        self._backoff_max_s = data_config.backoff_max_s
        self._transport = transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter
        self._client: httpx.AsyncClient | None = None
        self.limiter = TokenBucket.for_limit(
            source_config.limit_calls,
            source_config.limit_period_s,
            utilization=data_config.rate_limit_utilization,
            monotonic=monotonic,
            sleep=sleep,
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_s,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _backoff(self, attempt: int) -> float:
        delay = min(self._backoff_max_s, self._backoff_base_s * (2**attempt))
        # Up to 20% extra so parallel clients do not retry in lockstep.
        return delay * (1 + 0.2 * self._jitter())

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[httpx.Response, int]:
        """GET ``url``. Returns the response and its latency in milliseconds."""
        host = httpx.URL(url).host
        attempt = 0
        while True:
            await self.limiter.acquire()
            started = self._monotonic()
            failure: str
            status_code: int | None = None
            wait_s: float | None = None
            try:
                response = await self._http().get(url, params=params, headers=headers)
            except httpx.TimeoutException:
                failure = f"{host} no respondió en {self._timeout_s:g} s"
            except httpx.TransportError as exc:
                failure = f"no se pudo conectar con {host} ({type(exc).__name__})"
            else:
                latency_ms = round((self._monotonic() - started) * 1000)
                status_code = response.status_code
                if status_code < 400:
                    return response, latency_ms
                if status_code not in _RETRYABLE_STATUS:
                    raise SourceError(
                        _describe_http_error(host, status_code), status_code=status_code
                    )
                failure = _describe_http_error(host, status_code)
                wait_s = _retry_after_seconds(response)

            if attempt >= self._max_retries:
                raise SourceError(
                    f"{failure}, tras {attempt + 1} intentos", status_code=status_code
                )
            delay = (
                min(wait_s, self._backoff_max_s) if wait_s is not None else self._backoff(attempt)
            )
            log.warning(
                "source_retry",
                source=self.source,
                host=host,
                status_code=status_code,
                attempt=attempt + 1,
                wait_s=round(delay, 2),
            )
            if status_code == 429:
                # The provider says we are going too fast: everyone using this
                # source waits, not just this request.
                self.limiter.pause_for(delay)
            else:
                await self._sleep(delay)
            attempt += 1


def _describe_http_error(host: str, status_code: int) -> str:
    reasons = {
        401: "credenciales rechazadas",
        403: "acceso denegado",
        404: "no encontrado",
        429: "demasiadas solicitudes",
    }
    reason = reasons.get(status_code)
    if reason is None and status_code >= 500:
        reason = "error del servidor"
    return f"{host} respondió HTTP {status_code}" + (f" ({reason})" if reason else "")
