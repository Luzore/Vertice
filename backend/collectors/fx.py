"""USD/MXN exchange rate, to show the capital in pesos (informative only).

Banxico's FIX rate when a token is configured; otherwise the daily rate from FRED.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from collectors.base import Collector, RawSnapshot, _Requests
from collectors.fred import FredCollector, latest_observation
from core import clock

# SF43718: FIX exchange rate, pesos per US dollar.
BANXICO_FIX_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF43718/datos/oportuno"
FRED_SERIES = "DEXMXUS"


def latest_rate(snapshot: RawSnapshot) -> tuple[date, float] | None:
    """(date, pesos per dollar) from either source's payload."""
    if not snapshot.usable:
        return None
    if snapshot.source == "banxico":
        try:
            point = snapshot.payload["bmx"]["series"][0]["datos"][0]
            return (
                datetime.strptime(point["fecha"], "%d/%m/%Y").date(),  # noqa: DTZ007 - a date
                float(point["dato"].replace(",", "")),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return latest_observation(snapshot.payload)


class FxCollector(Collector):
    source = "banxico"

    async def usd_mxn(self, *, as_of: datetime | None = None) -> RawSnapshot:
        if not self.configured:
            moment = clock.ensure_utc(as_of) if as_of is not None else clock.now()
            return await FredCollector(self.runtime).series_observations(
                FRED_SERIES, start=clock.market_date(moment) - timedelta(days=30), as_of=as_of
            )

        async def fetch(requests: _Requests) -> Any:
            return await requests.get_json(
                BANXICO_FIX_URL, headers={"Bmx-Token": self._secret("banxico_token")}
            )

        return await self._collect("usd_mxn_fix", "SF43718", fetch, as_of=as_of)
