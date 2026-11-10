"""Massive (formerly Polygon.io): end-of-day bars, as a backup for Alpaca.

Optional. Free plan: 5 calls per minute, end of day only, 2 years of history.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from collectors.base import Collector, RawSnapshot, _Requests, request_key

BASE_URL = "https://api.massive.com"


class MassiveCollector(Collector):
    source = "massive"

    async def _get(self, requests: _Requests, path: str, params: dict[str, Any]) -> Any:
        return await requests.get_json(
            f"{BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._secret('massive_api_key')}"},
        )

    async def daily_bars(
        self, symbol: str, *, start: date, end: date, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Split-adjusted daily bars of one ticker."""
        ticker = symbol.upper()
        path = f"/v2/aggs/ticker/{quote(ticker, safe='')}/range/1/day/{start}/{end}"

        async def fetch(requests: _Requests) -> Any:
            return await self._get(
                requests, path, {"adjusted": "true", "sort": "asc", "limit": 50000}
            )

        return await self._collect(
            "daily_bars", request_key(symbol=ticker, start=start, end=end), fetch, as_of=as_of
        )

    async def grouped_daily(self, day: date, *, as_of: datetime | None = None) -> RawSnapshot:
        """The daily bar of every US stock for one session, in a single call."""
        path = f"/v2/aggs/grouped/locale/us/market/stocks/{day}"

        async def fetch(requests: _Requests) -> Any:
            return await self._get(requests, path, {"adjusted": "true"})

        return await self._collect("grouped_daily", request_key(day=day), fetch, as_of=as_of)
