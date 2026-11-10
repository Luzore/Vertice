"""Finnhub: company news, earnings dates and surprises, fundamentals and insiders.

Free plan, personal use: 60 calls per minute. Candles are not included, which is
why prices come from Alpaca.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from collectors.base import Collector, RawSnapshot, _Requests, request_key

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubCollector(Collector):
    source = "finnhub"

    async def _get(self, requests: _Requests, path: str, params: dict[str, Any]) -> Any:
        return await requests.get_json(
            f"{BASE_URL}{path}",
            params=params,
            headers={"X-Finnhub-Token": self._secret("finnhub_api_key")},
        )

    async def _simple(
        self, dataset: str, path: str, params: dict[str, Any], as_of: datetime | None
    ) -> RawSnapshot:
        async def fetch(requests: _Requests) -> Any:
            return await self._get(requests, path, params)

        return await self._collect(dataset, request_key(**params), fetch, as_of=as_of)

    async def company_news(
        self, symbol: str, *, start: date, end: date, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Headlines for one ticker between two dates (up to one year back)."""
        params = {"symbol": symbol.upper(), "from": start.isoformat(), "to": end.isoformat()}
        return await self._simple("company_news", "/company-news", params, as_of)

    async def earnings_calendar(
        self,
        *,
        start: date,
        end: date,
        symbol: str | None = None,
        as_of: datetime | None = None,
    ) -> RawSnapshot:
        """Who reports earnings between two dates, for the whole market or one ticker."""
        params: dict[str, Any] = {"from": start.isoformat(), "to": end.isoformat()}
        if symbol:
            params["symbol"] = symbol.upper()
        return await self._simple("earnings_calendar", "/calendar/earnings", params, as_of)

    async def earnings_surprises(
        self, symbol: str, *, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Actual versus estimated EPS for the last quarters."""
        params = {"symbol": symbol.upper()}
        return await self._simple("earnings_surprises", "/stock/earnings", params, as_of)

    async def basic_financials(self, symbol: str, *, as_of: datetime | None = None) -> RawSnapshot:
        """Market capitalization, beta, 52-week range and other basic metrics."""
        params = {"symbol": symbol.upper(), "metric": "all"}
        return await self._simple("basic_financials", "/stock/metric", params, as_of)

    async def company_profile(self, symbol: str, *, as_of: datetime | None = None) -> RawSnapshot:
        """Name, exchange, industry and market capitalization."""
        params = {"symbol": symbol.upper()}
        return await self._simple("company_profile", "/stock/profile2", params, as_of)

    async def insider_transactions(
        self, symbol: str, *, start: date, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Insider purchases and sales reported to the SEC (Form 4) since ``start``."""
        params = {"symbol": symbol.upper(), "from": start.isoformat()}
        return await self._simple(
            "insider_transactions", "/stock/insider-transactions", params, as_of
        )
