"""Alpaca: market data, asset list, news and splits.

Read-only by construction (principle 1). The only call to the trading host is the
list of tradable assets; there is no code here, or anywhere in the system, that
places, changes or cancels an order.

Daily bars use the consolidated tape (``sip``), which the free plan allows with a
15-minute delay, so volume is the real market volume. Intraday bars and quotes
use ``iex``, the only real-time feed on the free plan: prices are an approximation
of the consolidated price.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from collectors import freshness
from collectors.base import Collector, RawSnapshot, _Requests, request_key
from core import clock

DATA_URL = "https://data.alpaca.markets"
# Paper account host, used for the asset list only.
ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"

_SYMBOLS_PER_REQUEST = 100
_MAX_PAGES = 400


def _rfc3339(moment: datetime) -> str:
    return clock.ensure_utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(symbols: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(symbols), size):
        yield symbols[start : start + size]


def _normalize(symbols: Iterable[str]) -> list[str]:
    return sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})


class AlpacaCollector(Collector):
    source = "alpaca"

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._secret("alpaca_api_key"),
            "APCA-API-SECRET-KEY": self._secret("alpaca_api_secret"),
        }

    async def _bars(
        self, requests: _Requests, symbols: list[str], params: dict[str, Any]
    ) -> dict[str, list[dict]]:
        """All pages of a multi-symbol bars request, merged per symbol."""
        merged: dict[str, list[dict]] = {}
        for chunk in _chunks(symbols, _SYMBOLS_PER_REQUEST):
            page_token: str | None = None
            for _ in range(_MAX_PAGES):
                query = {**params, "symbols": ",".join(chunk), "limit": 10000, "sort": "asc"}
                if page_token:
                    query["page_token"] = page_token
                page = await requests.get_json(
                    f"{DATA_URL}/v2/stocks/bars", params=query, headers=self._headers()
                )
                for symbol, bars in (page.get("bars") or {}).items():
                    merged.setdefault(symbol, []).extend(bars)
                page_token = page.get("next_page_token")
                if not page_token:
                    break
        return merged

    async def daily_bars(
        self,
        symbols: Iterable[str],
        *,
        start: date,
        end: date | None = None,
        as_of: datetime | None = None,
    ) -> RawSnapshot:
        """Split-adjusted daily bars from ``start`` to ``end`` (default: today)."""
        tickers = _normalize(symbols)
        params: dict[str, Any] = {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "adjustment": "split",
            "feed": "sip",
        }
        if end is not None:
            params["end"] = end.isoformat()

        async def fetch(requests: _Requests) -> dict:
            bars = await self._bars(requests, tickers, params)
            return {"timeframe": "1Day", "adjustment": "split", "feed": "sip", "bars": bars}

        return await self._collect(
            "daily_bars",
            request_key(symbols=tickers, start=start, end=end),
            fetch,
            as_of=as_of,
            freshness=freshness.daily_bars(lambda payload: payload["bars"], until=end),
        )

    async def intraday_bars(
        self,
        symbols: Iterable[str],
        *,
        start: datetime,
        end: datetime | None = None,
        timeframe: str = "5Min",
        as_of: datetime | None = None,
    ) -> RawSnapshot:
        """Intraday bars (5 minutes by default) from the real-time IEX feed."""
        tickers = _normalize(symbols)
        params: dict[str, Any] = {
            "timeframe": timeframe,
            "start": _rfc3339(start),
            "adjustment": "split",
            "feed": "iex",
        }
        if end is not None:
            params["end"] = _rfc3339(end)

        async def fetch(requests: _Requests) -> dict:
            bars = await self._bars(requests, tickers, params)
            return {"timeframe": timeframe, "adjustment": "split", "feed": "iex", "bars": bars}

        return await self._collect(
            "intraday_bars",
            request_key(symbols=tickers, start=start, end=end, timeframe=timeframe),
            fetch,
            as_of=as_of,
        )

    async def latest_quotes(
        self, symbols: Iterable[str], *, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Latest bid and ask per symbol (IEX)."""
        tickers = _normalize(symbols)

        async def fetch(requests: _Requests) -> dict:
            merged: dict[str, dict] = {}
            for chunk in _chunks(tickers, _SYMBOLS_PER_REQUEST):
                page = await requests.get_json(
                    f"{DATA_URL}/v2/stocks/quotes/latest",
                    params={"symbols": ",".join(chunk), "feed": "iex"},
                    headers=self._headers(),
                )
                merged.update(page.get("quotes") or {})
            return {"feed": "iex", "quotes": merged}

        return await self._collect(
            "latest_quotes",
            request_key(symbols=tickers),
            fetch,
            as_of=as_of,
            freshness=freshness.quotes(
                lambda payload: payload["quotes"],
                max_age_s=self.runtime.config.stale.quote_max_age_s,
            ),
        )

    async def assets(self, *, as_of: datetime | None = None) -> RawSnapshot:
        """Every active US equity Alpaca knows; the universe is built from this list."""

        async def fetch(requests: _Requests) -> dict:
            assets = await requests.get_json(
                ASSETS_URL,
                params={"status": "active", "asset_class": "us_equity"},
                headers=self._headers(),
            )
            return {"assets": assets}

        return await self._collect("assets", "status=active", fetch, as_of=as_of)

    async def news(
        self,
        symbols: Iterable[str] | None = None,
        *,
        start: datetime,
        end: datetime | None = None,
        max_pages: int = 10,
        as_of: datetime | None = None,
    ) -> RawSnapshot:
        """Headlines and summaries, newest first. Without symbols: all market news."""
        tickers = _normalize(symbols) if symbols is not None else []
        params: dict[str, Any] = {
            "start": _rfc3339(start),
            "limit": 50,
            "sort": "desc",
            "include_content": "false",
        }
        if tickers:
            params["symbols"] = ",".join(tickers)
        if end is not None:
            params["end"] = _rfc3339(end)

        async def fetch(requests: _Requests) -> dict:
            items: list[dict] = []
            page_token: str | None = None
            truncated = False
            for page_number in range(max_pages):
                query = {**params, "page_token": page_token} if page_token else params
                page = await requests.get_json(
                    f"{DATA_URL}/v1beta1/news", params=query, headers=self._headers()
                )
                items.extend(page.get("news") or [])
                page_token = page.get("next_page_token")
                if not page_token:
                    break
                truncated = page_number == max_pages - 1
            return {"news": items, "truncated": truncated}

        return await self._collect(
            "news",
            request_key(symbols=tickers or "*", start=start, end=end, max_pages=max_pages),
            fetch,
            as_of=as_of,
        )

    async def corporate_actions(
        self,
        symbols: Iterable[str],
        *,
        start: date,
        end: date | None = None,
        as_of: datetime | None = None,
    ) -> RawSnapshot:
        """Forward and reverse splits announced for the symbols."""
        tickers = _normalize(symbols)
        params: dict[str, Any] = {
            "types": "forward_split,reverse_split",
            "start": start.isoformat(),
            "limit": 1000,
        }
        if end is not None:
            params["end"] = end.isoformat()

        async def fetch(requests: _Requests) -> dict:
            merged: dict[str, list[dict]] = {}
            for chunk in _chunks(tickers, _SYMBOLS_PER_REQUEST):
                page_token: str | None = None
                for _ in range(_MAX_PAGES):
                    query = {**params, "symbols": ",".join(chunk)}
                    if page_token:
                        query["page_token"] = page_token
                    page = await requests.get_json(
                        f"{DATA_URL}/v1/corporate-actions", params=query, headers=self._headers()
                    )
                    for kind, actions in (page.get("corporate_actions") or {}).items():
                        merged.setdefault(kind, []).extend(actions)
                    page_token = page.get("next_page_token")
                    if not page_token:
                        break
            return {"corporate_actions": merged}

        return await self._collect(
            "corporate_actions",
            request_key(symbols=tickers, start=start, end=end),
            fetch,
            as_of=as_of,
        )
