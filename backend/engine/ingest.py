"""Fetch data with the collectors, normalize it and store it (nodes 1 and 2 together).

Run by hand with ``python -m engine.ingest`` and, from Phase 5 on, by the scheduler.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from sqlalchemy import Engine, select, update

from collectors.alpaca import AlpacaCollector
from collectors.base import CollectorRuntime
from collectors.econ_calendar import EconCalendarCollector
from collectors.finnhub import FinnhubCollector
from collectors.fred import FredCollector
from collectors.registry import build_runtime
from collectors.sec_edgar import SecEdgarCollector
from core import clock, settings
from core.logging import configure_logging, get_logger
from core.settings import AppConfig
from db import models
from db.session import make_engine, session_scope
from engine import data_engine
from engine.sectors import SECTOR_NAMES, sector_etf_for_sic
from engine.universe import CONTEXT_TICKERS, eligible_assets, normalize_ticker

log = get_logger(__name__)

HISTORY_DAYS = 760  # a little over two years of sessions


@dataclass(frozen=True)
class UniverseReport:
    listed: int
    eligible: int
    liquid: int
    excluded: dict[str, int]


async def ingest_daily_bars(
    runtime: CollectorRuntime,
    engine: Engine,
    tickers: Sequence[str],
    *,
    start: date,
    end: date | None = None,
    batch: int = 400,
) -> int:
    """Download split-adjusted daily bars and store them. Returns bars stored."""
    alpaca = AlpacaCollector(runtime)
    stored = 0
    symbols = sorted(set(tickers))
    for offset in range(0, len(symbols), batch):
        snapshot = await alpaca.daily_bars(symbols[offset : offset + batch], start=start, end=end)
        if not snapshot.usable:
            log.error("daily_bars_failed", message=snapshot.message, offset=offset)
            continue
        bars, rejected = data_engine.parse_alpaca_bars(snapshot.payload)
        if rejected:
            log.warning("bars_rejected", count=rejected)
        with session_scope(engine) as session:
            stored += data_engine.store_bars(session, bars)
    return stored


async def refresh_universe(
    runtime: CollectorRuntime, engine: Engine, config: AppConfig
) -> UniverseReport:
    """Rebuild the universe: eligible common stocks that pass price and liquidity.

    Market capitalization (control C05) is checked later for the candidates only,
    because it costs one Finnhub call per company.
    """
    assets = await AlpacaCollector(runtime).assets()
    if not assets.usable:
        raise RuntimeError(f"no se pudo leer la lista de activos: {assets.message}")
    members, excluded = eligible_assets(assets.payload["assets"], config.universe)
    today = clock.market_date()
    await ingest_daily_bars(
        runtime, engine, [m.ticker for m in members], start=today - timedelta(days=45)
    )
    with session_scope(engine) as session:
        recent = data_engine.load_daily_bars(session, clock.now(), [m.ticker for m in members])
    liquid = []
    for member in members:
        bars = recent.get(member.ticker)
        if bars is None or len(bars) < 15:
            continue
        window = bars.tail(20)
        dollar_volume = float(np.mean(window["close"] * window["volume"]))
        if (
            window["close"].iloc[-1] >= config.universe.min_price
            and dollar_volume >= config.universe.min_dollar_volume_20d
        ):
            liquid.append(member)

    now = clock.now()
    with session_scope(engine) as session:
        known = {row.ticker: row for row in session.scalars(select(models.Universe)).all()}
        session.execute(update(models.Universe).values(active=False))
        for member in liquid:
            row = known.get(member.ticker)
            if row is None:
                session.add(
                    models.Universe(
                        ticker=member.ticker,
                        name=member.name,
                        exchange=member.exchange,
                        active=True,
                        updated_at=now,
                    )
                )
            else:
                row.name, row.exchange, row.active, row.updated_at = (
                    member.name,
                    member.exchange,
                    True,
                    now,
                )
    counts: dict[str, int] = {}
    for verdict in excluded.values():
        counts[verdict.category] = counts.get(verdict.category, 0) + 1
    return UniverseReport(
        listed=len(members) + len(excluded),
        eligible=len(members),
        liquid=len(liquid),
        excluded=counts,
    )


async def refresh_sectors(
    runtime: CollectorRuntime, engine: Engine, *, limit: int | None = None, concurrency: int = 6
) -> int:
    """Fill in SEC identifier, SIC code and sector ETF for universe members that
    lack them. Resumable: each run continues with the ones still missing."""
    sec = SecEdgarCollector(runtime)
    tickers_snapshot = await sec.company_tickers()
    if not tickers_snapshot.usable:
        log.error("sec_tickers_failed", message=tickers_snapshot.message)
        return 0
    cik_by_ticker = {
        normalize_ticker(row["ticker"]): int(row["cik_str"])
        for row in tickers_snapshot.payload.values()
    }
    with session_scope(engine) as session:
        pending = session.scalars(
            select(models.Universe.ticker)
            .where(models.Universe.active.is_(True), models.Universe.sic.is_(None))
            .order_by(models.Universe.ticker)
        ).all()
    pending = [t for t in pending if t in cik_by_ticker][:limit]
    gate = asyncio.Semaphore(concurrency)
    results: dict[str, tuple[int, int | None]] = {}

    async def fetch(ticker: str) -> None:
        async with gate:
            snapshot = await sec.submissions(cik_by_ticker[ticker])
        if snapshot.usable:
            sic = snapshot.payload.get("sic")
            results[ticker] = (
                cik_by_ticker[ticker],
                int(sic) if str(sic or "").isdigit() else None,
            )

    await asyncio.gather(*(fetch(ticker) for ticker in pending))
    now = clock.now()
    with session_scope(engine) as session:
        for ticker, (cik, sic) in results.items():
            row = session.get(models.Universe, ticker)
            etf = sector_etf_for_sic(sic)
            row.cik, row.sic, row.sector_etf = cik, sic if sic is not None else 0, etf
            row.sector = SECTOR_NAMES.get(etf) if etf else None
            row.profile_updated_at = now
    return len(results)


async def ingest_macro(runtime: CollectorRuntime, engine: Engine, config: AppConfig) -> int:
    """Macro series from FRED and the economic calendar."""
    fred = FredCollector(runtime)
    start = clock.market_date() - timedelta(days=420)
    stored = 0
    for series_id in config.data.fred_series:
        snapshot = await fred.series_observations(series_id, start=start)
        if snapshot.usable:
            with session_scope(engine) as session:
                stored += data_engine.store_macro(
                    session, data_engine.parse_fred_series(snapshot.payload, series_id)
                )
    calendar = await EconCalendarCollector(runtime).upcoming()
    if calendar.usable:
        with session_scope(engine) as session:
            data_engine.store_econ_events(
                session, data_engine.parse_econ_calendar(calendar.payload)
            )
    return stored


async def ingest_earnings_calendar(
    runtime: CollectorRuntime, engine: Engine, *, days_ahead: int = 21
) -> int:
    """Scheduled earnings for the whole market in one call."""
    today = clock.market_date()
    snapshot = await FinnhubCollector(runtime).earnings_calendar(
        start=today - timedelta(days=3), end=today + timedelta(days=days_ahead)
    )
    if not snapshot.usable:
        return 0
    with session_scope(engine) as session:
        known = set(session.scalars(select(models.Universe.ticker)).all())
        events = [
            event
            for event in data_engine.parse_finnhub_earnings_calendar(snapshot.payload)
            if event.ticker in known
        ]
        return data_engine.store_corporate_events(session, events)


async def _run(args: argparse.Namespace) -> int:
    config, secrets = settings.get_config(), settings.get_secrets()
    engine = make_engine()
    runtime = build_runtime(config, secrets, engine=engine)
    try:
        if args.what in ("universe", "all"):
            report = await refresh_universe(runtime, engine, config)
            print(
                f"universo: {report.listed} listados, {report.eligible} acciones comunes, "
                f"{report.liquid} pasan precio y liquidez; excluidos: {report.excluded}"
            )
            done = await refresh_sectors(runtime, engine, limit=args.limit)
            print(f"sectores asignados a {done} empresas")
        if args.what in ("bars", "all"):
            with session_scope(engine) as session:
                tickers = list(
                    session.scalars(
                        select(models.Universe.ticker).where(models.Universe.active.is_(True))
                    ).all()
                )
            tickers = tickers[: args.limit] if args.limit else tickers
            start = clock.market_date() - timedelta(days=args.days)
            count = await ingest_daily_bars(
                runtime, engine, [*tickers, *CONTEXT_TICKERS], start=start
            )
            print(
                f"barras diarias guardadas: {count} "
                f"({len(tickers)} acciones y {len(CONTEXT_TICKERS)} ETFs)"
            )
        if args.what in ("macro", "all"):
            print(f"observaciones macro guardadas: {await ingest_macro(runtime, engine, config)}")
            print(
                f"fechas de resultados guardadas: {await ingest_earnings_calendar(runtime, engine)}"
            )
    finally:
        await runtime.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.ingest", description="Descarga y guarda datos de mercado."
    )
    parser.add_argument("what", choices=["universe", "bars", "macro", "all"])
    parser.add_argument("--days", type=int, default=HISTORY_DAYS, help="días de historia de barras")
    parser.add_argument("--limit", type=int, default=None, help="máximo de acciones (para pruebas)")
    args = parser.parse_args(argv)
    configure_logging(
        "WARNING", secret_values=settings.get_secrets().secret_values(), stream=sys.stderr
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
