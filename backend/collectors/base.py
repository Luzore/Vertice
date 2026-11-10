"""The collector contract (ROADMAP section 6, node 1).

A collector fetches raw data from one source and returns a ``RawSnapshot``: source,
time asked, latency, status and the raw payload. It never raises because a source
is down: the failure comes back as a snapshot with status ``error`` so the rest of
the pipeline keeps running and the UI can say which source failed and why.

Turning payloads into clean, typed records is the data engine's job (node 2).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol

import httpx2 as httpx

from collectors.http import SourceClient, SourceError
from core import clock
from core.cache import TTLCache
from core.logging import get_logger, scrub_text
from core.settings import SOURCE_SECRETS, DataConfig, Secrets

log = get_logger(__name__)


class SnapshotStatus(StrEnum):
    OK = "ok"
    SLOW = "slow"
    ERROR = "error"
    STALE = "stale"
    # The source has no credentials. Nothing was requested and nothing is stored.
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class RawSnapshot:
    source: str
    dataset: str
    request_key: str
    # Clock time the data was requested for (simulated in replay).
    as_of: datetime
    # Real time the request was made.
    fetched_at: datetime
    status: SnapshotStatus
    latency_ms: int | None = None
    payload: Any | None = None
    # Why the status is error, stale, slow or not configured (Spanish, shown in the UI).
    message: str | None = None
    from_cache: bool = False

    @property
    def usable(self) -> bool:
        """True when there is data to work with; ``status`` says how good it is."""
        return self.payload is not None


class Recorder(Protocol):
    """Receives every fresh snapshot (to store it and update the source status)."""

    async def record(self, snapshot: RawSnapshot) -> None: ...


class _Requests:
    """HTTP access handed to one fetch; adds up latency across its requests."""

    def __init__(self, client: SourceClient) -> None:
        self._client = client
        self.latencies_ms: list[int] = []

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        response, latency_ms = await self._client.get(url, **kwargs)
        self.latencies_ms.append(latency_ms)
        return response

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            host = httpx.URL(url).host
            raise SourceError(f"{host} devolvió una respuesta que no es JSON") from exc

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.get(url, **kwargs)).text


Fetch = Callable[[_Requests], Awaitable[Any]]
# Returns a reason (Spanish) when the payload is too old for ``as_of``, else None.
Freshness = Callable[[Any, datetime], str | None]


@dataclass
class CollectorRuntime:
    """What collectors share: configuration, credentials, HTTP clients and cache."""

    config: DataConfig
    secrets: Secrets
    recorder: Recorder | None = None
    # FOMC decision days from config.yaml, for the economic calendar.
    fomc_dates: tuple[date, ...] = ()
    transport: httpx.AsyncBaseTransport | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    cache: TTLCache[RawSnapshot] = field(init=False)
    _clients: dict[str, SourceClient] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.cache = TTLCache(monotonic=self.monotonic)

    def client(self, source: str) -> SourceClient:
        """The one client (and one rate limiter) for ``source``."""
        if source not in self._clients:
            self._clients[source] = SourceClient(
                source,
                self.config.sources[source],
                self.config,
                transport=self.transport,
                sleep=self.sleep,
                monotonic=self.monotonic,
                jitter=self.jitter,
            )
        return self._clients[source]

    def scrub(self, text: str) -> str:
        return scrub_text(text, self.secrets.secret_values())

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


def request_key(**params: Any) -> str:
    """Canonical description of a request, e.g. ``end=2026-11-10&symbols=AAPL,SPY``."""

    def render(value: Any) -> str:
        if isinstance(value, datetime):
            return clock.ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            return value
        if isinstance(value, Iterable):
            return ",".join(sorted(render(item) for item in value))
        return str(value)

    return "&".join(
        f"{name}={render(value)}" for name, value in sorted(params.items()) if value is not None
    )


def _cache_key(source: str, dataset: str, key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    return f"{source}:{dataset}:{digest}"


class Collector:
    """Base class: configuration check, cache, status and recording for every fetch."""

    source: ClassVar[str]

    def __init__(self, runtime: CollectorRuntime) -> None:
        self.runtime = runtime
        self.http = runtime.client(self.source)

    @property
    def secrets(self) -> Secrets:
        return self.runtime.secrets

    @property
    def missing_credentials(self) -> list[str]:
        return self.secrets.missing_for(self.source)

    @property
    def configured(self) -> bool:
        return not self.missing_credentials

    def _secret(self, name: str) -> str:
        assert name in SOURCE_SECRETS[self.source], f"{name} is not declared for {self.source}"
        return getattr(self.secrets, name).get_secret_value()

    async def _collect(
        self,
        dataset: str,
        key: str,
        fetch: Fetch,
        *,
        as_of: datetime | None = None,
        freshness: Freshness | None = None,
    ) -> RawSnapshot:
        as_of = clock.ensure_utc(as_of) if as_of is not None else clock.now()
        base = RawSnapshot(
            source=self.source,
            dataset=dataset,
            request_key=key,
            as_of=as_of,
            fetched_at=datetime.now(UTC),
            status=SnapshotStatus.OK,
        )
        if not self.configured:
            missing = ", ".join(self.missing_credentials)
            return replace(
                base,
                status=SnapshotStatus.NOT_CONFIGURED,
                message=f"sin configurar: falta {missing}",
            )

        cache_key = _cache_key(self.source, dataset, key)
        cached = self.runtime.cache.get(cache_key)
        if cached is not None:
            return replace(cached, from_cache=True)

        requests = _Requests(self.http)
        try:
            payload = await fetch(requests)
        except SourceError as exc:
            snapshot = replace(
                base,
                status=SnapshotStatus.ERROR,
                message=self.runtime.scrub(str(exc)),
                latency_ms=sum(requests.latencies_ms) if requests.latencies_ms else None,
            )
            log.error("collector_failed", source=self.source, dataset=dataset, error=str(exc))
        else:
            latency_ms = sum(requests.latencies_ms) if requests.latencies_ms else None
            slowest = max(requests.latencies_ms, default=0)
            stale_reason = freshness(payload, as_of) if freshness is not None else None
            if stale_reason:
                status, message = SnapshotStatus.STALE, stale_reason
            elif slowest > self.runtime.config.slow_ms:
                status, message = SnapshotStatus.SLOW, f"respuesta lenta: {slowest} ms"
            else:
                status, message = SnapshotStatus.OK, None
            snapshot = replace(
                base, status=status, message=message, latency_ms=latency_ms, payload=payload
            )
            if status in (SnapshotStatus.OK, SnapshotStatus.SLOW):
                ttl = self.runtime.config.sources[self.source].cache_ttl_s.get(dataset, 0)
                self.runtime.cache.set(cache_key, snapshot, ttl)

        if self.runtime.recorder is not None:
            await self.runtime.recorder.record(snapshot)
        return snapshot
