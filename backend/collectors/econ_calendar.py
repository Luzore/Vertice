"""Economic calendar: high-impact US releases and FOMC decisions.

Release dates (CPI, employment, GDP) come from FRED's release calendar; FRED gives
the date, and the time of day is fixed in config.yaml. FOMC decision days come
from the list in config.yaml, so they are available even when FRED is not.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from collectors.base import Collector, RawSnapshot, SnapshotStatus
from collectors.fred import FredCollector
from core import clock

DATASET = "econ_calendar"


class EconCalendarCollector(Collector):
    source = "fred"

    def _fomc_events(self, first: date, last: date) -> list[dict]:
        return [
            {
                "name": "FOMC",
                "country": "US",
                "scheduled_at": clock.market_time(
                    day, self.runtime.config.fomc_time_et
                ).isoformat(),
                "impact": "high",
                "source": "config",
            }
            for day in self.runtime.fomc_dates
            if first <= day <= last
        ]

    async def upcoming(
        self, *, horizon_days: int = 60, as_of: datetime | None = None
    ) -> RawSnapshot:
        """Events from today to ``horizon_days`` ahead, soonest first."""
        moment = clock.ensure_utc(as_of) if as_of is not None else clock.now()
        first = clock.market_date(moment)
        last = first + timedelta(days=horizon_days)
        events = self._fomc_events(first, last)
        snapshot = RawSnapshot(
            source=self.source,
            dataset=DATASET,
            request_key=f"horizon_days={horizon_days}",
            as_of=moment,
            fetched_at=datetime.now(UTC),
            status=SnapshotStatus.OK,
        )

        if not self.configured:
            missing = ", ".join(self.missing_credentials)
            return replace(
                snapshot,
                status=SnapshotStatus.NOT_CONFIGURED,
                message=f"sin configurar: falta {missing}; solo se conocen las fechas FOMC",
                payload={
                    "events": sorted(events, key=lambda e: e["scheduled_at"]),
                    "partial": True,
                },
            )

        fred = FredCollector(self.runtime)
        failures: list[str] = []
        latencies: list[int] = []
        for release in self.runtime.config.econ_releases:
            dates = await fred.release_dates(release.fred_release_id, as_of=moment)
            if not dates.usable:
                failures.append(f"{release.name}: {dates.message}")
                continue
            if dates.latency_ms is not None and not dates.from_cache:
                latencies.append(dates.latency_ms)
            for entry in dates.payload.get("release_dates") or []:
                day = date.fromisoformat(entry["date"])
                if first <= day <= last:
                    events.append(
                        {
                            "name": release.name,
                            "country": "US",
                            "scheduled_at": clock.market_time(day, release.time_et).isoformat(),
                            "impact": release.impact,
                            "source": "fred",
                        }
                    )

        snapshot = replace(
            snapshot,
            status=SnapshotStatus.ERROR if failures else SnapshotStatus.OK,
            message="calendario incompleto; " + "; ".join(failures) if failures else None,
            latency_ms=sum(latencies) if latencies else None,
            payload={
                "events": sorted(events, key=lambda e: e["scheduled_at"]),
                "partial": bool(failures),
            },
        )
        if self.runtime.recorder is not None:
            await self.runtime.recorder.record(snapshot)
        return snapshot
