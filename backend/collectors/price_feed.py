"""Live prices for the watcher (ROADMAP section 12).

Two sources behind one interface:

* ``AlpacaStream``: the WebSocket of Alpaca (IEX), up to 30 symbols. Trades arrive
  as they happen and are kept as "last price plus the extremes since the last
  read", so the watcher can evaluate every ten seconds without missing a dip.
* ``PollingFeed``: the same prices asked for over HTTPS every few seconds. It is
  what runs where WebSockets are not allowed, and the fallback when the stream
  drops.

IEX prices are an approximation of the consolidated price; every tick carries its
source and its time so the alert can show them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from collectors.alpaca import AlpacaCollector
from collectors.base import SnapshotStatus
from core import clock, market_calendar
from core.logging import get_logger
from engine.data_engine import _parse_instant
from engine.watcher import Tick

log = get_logger(__name__)

STREAM_URL = "wss://stream.data.alpaca.markets/v2/iex"
MAX_SYMBOLS = 30
SOURCE = "alpaca:iex"
# A first price within this long of the open counts as the opening price.
OPEN_WINDOW = timedelta(minutes=10)
STREAM_SILENCE_LIMIT_S = 90


class PriceFeed(Protocol):
    async def poll(self, tickers: Iterable[str]) -> list[Tick]:
        """New prices of ``tickers`` since the last call (nothing when there are none)."""
        ...


def _is_open_tick(ts: datetime) -> bool:
    session = market_calendar.get_session(clock.market_date(ts))
    return session is not None and session.open <= ts <= session.open + OPEN_WINDOW


class PollingFeed:
    """Latest trades over HTTPS. Returns a tick only when the trade is new."""

    def __init__(self, alpaca: AlpacaCollector) -> None:
        self._alpaca = alpaca
        self._last_trade: dict[str, datetime] = {}
        self._seen_today: dict[str, Any] = {}
        # Why the last poll gave nothing, in Spanish; None when it worked.
        self.problem: str | None = None

    async def poll(self, tickers: Iterable[str]) -> list[Tick]:
        wanted = sorted(set(tickers))[:MAX_SYMBOLS]
        if not wanted:
            return []
        snapshot = await self._alpaca.snapshots(wanted)
        if snapshot.status not in (SnapshotStatus.OK, SnapshotStatus.SLOW):
            self.problem = snapshot.message or snapshot.status.value
            return []
        self.problem = None
        ticks = []
        for symbol, row in (snapshot.payload or {}).get("snapshots", {}).items():
            trade = (row or {}).get("latestTrade") or {}
            if not trade.get("p") or not trade.get("t"):
                continue
            ts = _parse_instant(trade["t"])
            if self._last_trade.get(symbol) == ts:
                continue
            self._last_trade[symbol] = ts
            day = clock.market_date(ts)
            first_today = self._seen_today.get(symbol) != day
            self._seen_today[symbol] = day
            ticks.append(
                Tick(
                    symbol,
                    float(trade["p"]),
                    ts,
                    SOURCE,
                    is_open=first_today and _is_open_tick(ts),
                )
            )
        return ticks


@dataclass
class _Pending:
    price: float
    ts: datetime
    high: float
    low: float
    is_open: bool


class AlpacaStream:
    """The WebSocket of Alpaca: authenticates, subscribes to trades and keeps the
    latest price of each symbol with the extremes seen since it was last read."""

    def __init__(
        self,
        key: str,
        secret: str,
        *,
        url: str = STREAM_URL,
        connect: Callable[[str], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key, self._secret, self._url = key, secret, url
        self._connect = connect
        self._sleep = sleep
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._pending: dict[str, _Pending] = {}
        self._seen_today: dict[str, Any] = {}
        self._socket: Any = None
        self.authenticated = False
        self.last_message_at: datetime | None = None
        self.problem: str | None = "sin conectar"

    @property
    def healthy(self) -> bool:
        """Connected, authenticated and heard from recently."""
        if not self.authenticated or self.last_message_at is None:
            return False
        return (clock.now() - self.last_message_at).total_seconds() <= STREAM_SILENCE_LIMIT_S

    def subscribe(self, tickers: Iterable[str]) -> None:
        """The symbols to follow from now on (at most 30)."""
        self._wanted = set(sorted(set(tickers))[:MAX_SYMBOLS])

    def handle(self, message: dict) -> None:
        """Apply one message of the stream."""
        self.last_message_at = clock.now()
        kind = message.get("T")
        if kind == "success" and message.get("msg") == "authenticated":
            self.authenticated, self.problem = True, None
        elif kind == "error":
            self.authenticated = False
            self.problem = (
                f"Alpaca rechazó la conexión: {message.get('msg')} ({message.get('code')})"
            )
            log.warning("stream_error", code=message.get("code"))
        elif kind == "t" and message.get("S") and message.get("p") and message.get("t"):
            symbol, price = message["S"], float(message["p"])
            ts = _parse_instant(message["t"])
            day = clock.market_date(ts)
            first_today = self._seen_today.get(symbol) != day
            self._seen_today[symbol] = day
            pending = self._pending.get(symbol)
            if pending is None:
                self._pending[symbol] = _Pending(
                    price, ts, price, price, first_today and _is_open_tick(ts)
                )
            else:
                pending.price, pending.ts = price, ts
                pending.high, pending.low = max(pending.high, price), min(pending.low, price)

    async def poll(self, tickers: Iterable[str]) -> list[Tick]:
        ticks = []
        for symbol in sorted(set(tickers)):
            pending = self._pending.pop(symbol, None)
            if pending is not None:
                ticks.append(
                    Tick(symbol, pending.price, pending.ts, SOURCE, pending.is_open,
                         pending.high, pending.low)
                )  # fmt: skip
        return ticks

    async def _sync_subscription(self) -> None:
        add, drop = self._wanted - self._subscribed, self._subscribed - self._wanted
        if drop:
            await self._socket.send(json.dumps({"action": "unsubscribe", "trades": sorted(drop)}))
        if add:
            await self._socket.send(json.dumps({"action": "subscribe", "trades": sorted(add)}))
        self._subscribed = set(self._wanted)

    async def _session(self) -> None:
        """One connection: authenticate, subscribe and read until it closes."""
        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect
        async with connect(self._url) as socket:
            self._socket, self._subscribed = socket, set()
            await socket.send(
                json.dumps({"action": "auth", "key": self._key, "secret": self._secret})
            )
            async for raw in socket:
                for message in json.loads(raw):
                    self.handle(message)
                if self.authenticated and self._wanted != self._subscribed:
                    await self._sync_subscription()

    async def run(self, *, max_attempts: int | None = None) -> None:
        """Keep the stream connected, waiting longer after each failure (up to a
        minute). Runs until cancelled, or until ``max_attempts`` connections ended."""
        attempt = 0
        while max_attempts is None or attempt < max_attempts:
            try:
                await self._session()
                self.problem = "la conexión se cerró"
            except asyncio.CancelledError:
                raise
            except Exception as error:  # any network failure: retry
                self.problem = f"sin conexión con el stream de precios ({type(error).__name__})"
            finally:
                self.authenticated, self._socket = False, None
            attempt += 1
            await self._sleep(min(60.0, 2.0**attempt))


class FallbackFeed:
    """The stream while it is healthy, polling otherwise."""

    def __init__(self, stream: AlpacaStream | None, polling: PollingFeed) -> None:
        self._stream, self._polling = stream, polling

    @property
    def mode(self) -> str:
        return "stream" if self._stream is not None and self._stream.healthy else "polling"

    @property
    def problem(self) -> str | None:
        if self.mode == "stream":
            return None
        return self._polling.problem

    async def poll(self, tickers: Iterable[str]) -> list[Tick]:
        wanted = list(tickers)
        if self._stream is not None:
            self._stream.subscribe(wanted)
            if self._stream.healthy:
                return await self._stream.poll(wanted)
        return await self._polling.poll(wanted)
