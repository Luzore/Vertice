"""FRED (St. Louis Fed): macro series and the calendar of economic releases.

FRED only accepts the key as a query parameter, so it travels in the URL. Error
messages and logs never include the URL for that reason.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from collectors import freshness
from collectors.base import Collector, RawSnapshot, _Requests, request_key

BASE_URL = "https://api.stlouisfed.org/fred"


def latest_observation(payload: dict) -> tuple[date, float] | None:
    """Newest observation that has a value (FRED marks missing days with '.')."""
    for observation in reversed(payload.get("observations") or []):
        if observation.get("value") not in (None, "", "."):
            return date.fromisoformat(observation["date"]), float(observation["value"])
    return None


def _latest_date(payload: dict) -> date | None:
    latest = latest_observation(payload)
    return latest[0] if latest else None


class FredCollector(Collector):
    source = "fred"

    async def _get(self, requests: _Requests, path: str, params: dict[str, Any]) -> Any:
        query = {**params, "api_key": self._secret("fred_api_key"), "file_type": "json"}
        return await requests.get_json(f"{BASE_URL}{path}", params=query)

    async def series_observations(
        self, series_id: str, *, start: date, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Observations of one series from ``start``, oldest first."""
        params = {
            "series_id": series_id,
            "observation_start": start.isoformat(),
            "sort_order": "asc",
        }

        async def fetch(requests: _Requests) -> Any:
            return await self._get(requests, "/series/observations", params)

        stale = self.runtime.config.stale
        max_age = stale.macro_max_age_overrides.get(series_id, stale.macro_max_age_trading_days)
        return await self._collect(
            "series_observations",
            request_key(series_id=series_id, start=start),
            fetch,
            as_of=as_of,
            freshness=freshness.macro_series(_latest_date, max_age_trading_days=max_age),
        )

    async def release_dates(
        self, release_id: int, *, limit: int = 24, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Publication dates of a release, newest first, including scheduled ones."""
        params = {
            "release_id": release_id,
            "include_release_dates_with_no_data": "true",
            "sort_order": "desc",
            "limit": limit,
        }

        async def fetch(requests: _Requests) -> Any:
            return await self._get(requests, "/release/dates", params)

        return await self._collect(
            "release_dates", request_key(release_id=release_id, limit=limit), fetch, as_of=as_of
        )
