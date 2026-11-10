"""Yahoo Finance chart data: backup for the intraday VIX only.

Unofficial and frequently blocked (HTTP 429). Never a primary source and never
critical: when it fails, the system uses the daily VIX from FRED and says so.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from collectors.base import Collector, RawSnapshot, _Requests, request_key
from collectors.http import SourceError

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36"
    )
}


class YahooCollector(Collector):
    source = "yahoo"

    async def chart(
        self,
        symbol: str = "^VIX",
        *,
        interval: str = "15m",
        period: str = "1d",
        as_of: datetime | None = None,
    ) -> RawSnapshot:
        """Recent prices of an index such as ^VIX."""

        async def fetch(requests: _Requests) -> dict:
            payload = await requests.get_json(
                CHART_URL.format(symbol=quote(symbol, safe="")),
                params={"interval": interval, "range": period},
                headers=_HEADERS,
            )
            results = (
                (payload.get("chart") or {}).get("result") if isinstance(payload, dict) else None
            )
            if not results:
                raise SourceError("Yahoo no devolvió datos para el símbolo")
            return payload

        return await self._collect(
            "chart",
            request_key(symbol=symbol, interval=interval, period=period),
            fetch,
            as_of=as_of,
        )
