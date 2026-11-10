"""``python -m collectors.check``: test every data source and show its status.

Makes one small real request per source through the same code the system uses
(rate limiter, retries, stale-data checks) and prints a table with status and
latency. Sources without credentials show as SIN CONFIGURAR and are not contacted.

Exit code 0 when every critical source works, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from alembic.migration import MigrationContext
from sqlalchemy import Engine

from collectors.alpaca import AlpacaCollector
from collectors.base import CollectorRuntime, RawSnapshot, SnapshotStatus
from collectors.econ_calendar import EconCalendarCollector
from collectors.finnhub import FinnhubCollector
from collectors.fred import FredCollector, latest_observation
from collectors.fx import FxCollector, latest_rate
from collectors.health import sync_configuration
from collectors.massive import MassiveCollector
from collectors.registry import build_runtime
from collectors.rss_news import RssNewsCollector, parse_rss
from collectors.sec_edgar import SecEdgarCollector
from collectors.telegram import TelegramCollector
from collectors.yahoo import YahooCollector
from core import clock, settings
from core.logging import configure_logging
from core.settings import DataConfig
from db.migrate import head_revision
from db.session import make_engine, session_scope

STATUS_LABELS = {
    "ok": "OK",
    "slow": "LENTA",
    "stale": "DATOS VIEJOS",
    "error": "ERROR",
    "not_configured": "SIN CONFIGURAR",
}
APPLE_CIK = 320193


@dataclass(frozen=True)
class CheckResult:
    source: str
    label: str
    status: str
    latency_ms: int | None
    detail: str
    critical: bool

    @property
    def healthy(self) -> bool:
        return self.status in ("ok", "slow")


Describe = Callable[[RawSnapshot], str]


def _result(
    config: DataConfig,
    label: str,
    snapshot: RawSnapshot,
    describe: Describe,
    *,
    source: str | None = None,
) -> CheckResult:
    source = source or snapshot.source
    if snapshot.status in (SnapshotStatus.OK, SnapshotStatus.SLOW):
        detail = describe(snapshot)
    elif snapshot.status is SnapshotStatus.STALE:
        detail = snapshot.message or "datos viejos"
    else:
        detail = (snapshot.message or "").removeprefix("sin configurar: ")
    return CheckResult(
        source=source,
        label=label,
        status=snapshot.status.value,
        latency_ms=snapshot.latency_ms,
        detail=detail,
        critical=config.sources[source].critical,
    )


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _new_york(iso_instant: str) -> datetime:
    return clock.to_market(datetime.fromisoformat(iso_instant))


async def _check_alpaca(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    alpaca = AlpacaCollector(runtime)
    quotes, bars = await asyncio.gather(
        alpaca.latest_quotes(["SPY"], as_of=moment),
        alpaca.daily_bars(
            ["SPY"], start=clock.market_date(moment) - timedelta(days=10), as_of=moment
        ),
    )

    def describe_quote(snapshot: RawSnapshot) -> str:
        quote = snapshot.payload["quotes"].get("SPY") or {}
        return f"SPY compra {_money(quote.get('bp', 0))} / venta {_money(quote.get('ap', 0))} (IEX)"

    def describe_bars(snapshot: RawSnapshot) -> str:
        spy = snapshot.payload["bars"].get("SPY") or []
        if not spy:
            return "sin barras"
        return (
            f"{len(spy)} barras de SPY; última {spy[-1]['t'][:10]}, cierre {_money(spy[-1]['c'])}"
        )

    return [
        _result(runtime.config, "precios", quotes, describe_quote),
        _result(runtime.config, "barras diarias", bars, describe_bars),
    ]


async def _check_finnhub(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    finnhub = FinnhubCollector(runtime)
    today = clock.market_date(moment)
    profile, earnings = await asyncio.gather(
        finnhub.company_profile("AAPL", as_of=moment),
        finnhub.earnings_calendar(start=today, end=today + timedelta(days=7), as_of=moment),
    )

    def describe_profile(snapshot: RawSnapshot) -> str:
        data = snapshot.payload or {}
        cap_billions = (data.get("marketCapitalization") or 0) / 1000
        return f"{data.get('name', '?')}: capitalización ${cap_billions:,.0f} mil millones"

    def describe_earnings(snapshot: RawSnapshot) -> str:
        count = len((snapshot.payload or {}).get("earningsCalendar") or [])
        return f"{count} empresas reportan resultados en los próximos 7 días"

    return [
        _result(runtime.config, "perfil de empresa", profile, describe_profile),
        _result(runtime.config, "calendario de resultados", earnings, describe_earnings),
    ]


async def _check_fred(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    today = clock.market_date(moment)
    vix, calendar = await asyncio.gather(
        FredCollector(runtime).series_observations(
            "VIXCLS", start=today - timedelta(days=20), as_of=moment
        ),
        EconCalendarCollector(runtime).upcoming(as_of=moment),
    )

    def describe_vix(snapshot: RawSnapshot) -> str:
        latest = latest_observation(snapshot.payload)
        return f"VIX {latest[1]:.2f} al {latest[0]}" if latest else "sin observaciones"

    def describe_calendar(snapshot: RawSnapshot) -> str:
        events = snapshot.payload["events"]
        if not events:
            return "sin eventos de alto impacto en 60 días"
        upcoming = ", ".join(
            f"{event['name']} {_new_york(event['scheduled_at']):%d-%b %H:%M}"
            for event in events[:4]
        )
        return f"{len(events)} eventos; próximos (hora NY): {upcoming}"

    return [
        _result(runtime.config, "VIX diario", vix, describe_vix),
        _result(runtime.config, "calendario económico", calendar, describe_calendar),
    ]


async def _check_sec(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    snapshot = await SecEdgarCollector(runtime).submissions(APPLE_CIK, as_of=moment)

    def describe(snap: RawSnapshot) -> str:
        recent = snap.payload["filings"]["recent"]
        name = snap.payload.get("name", "?")
        return f"{name}: último filing {recent['form'][0]} del {recent['filingDate'][0]}"

    return [_result(runtime.config, "filings", snapshot, describe)]


async def _check_rss(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    rss = RssNewsCollector(runtime)
    google, yahoo = await asyncio.gather(
        rss.google_news("stock market", as_of=moment), rss.yahoo_finance("SPY", as_of=moment)
    )

    def describe(snap: RawSnapshot) -> str:
        return f"{len(parse_rss(snap.payload['xml']))} titulares"

    return [
        _result(runtime.config, "Google News", google, describe),
        _result(runtime.config, "Yahoo Finance RSS", yahoo, describe),
    ]


async def _check_fx(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    fx = FxCollector(runtime)
    snapshot = await fx.usd_mxn(as_of=moment)
    rate = latest_rate(snapshot)
    rate_text = f"{rate[1]:.4f} pesos por dólar al {rate[0]}" if rate else "sin dato"
    critical = runtime.config.sources["banxico"].critical
    missing = ", ".join(fx.missing_credentials)
    if fx.configured:
        return [_result(runtime.config, "USD/MXN (FIX)", snapshot, lambda _: rate_text)]
    return [
        CheckResult(
            source="banxico",
            label="USD/MXN (FIX)",
            status="not_configured",
            latency_ms=None,
            detail=f"falta {missing} (opcional); respaldo FRED: {rate_text}",
            critical=critical,
        )
    ]


async def _check_massive(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    today = clock.market_date(moment)
    snapshot = await MassiveCollector(runtime).daily_bars(
        "SPY", start=today - timedelta(days=10), end=today, as_of=moment
    )

    def describe(snap: RawSnapshot) -> str:
        return f"{len(snap.payload.get('results') or [])} barras de SPY"

    result = _result(runtime.config, "barras de respaldo", snapshot, describe)
    if result.status == "not_configured":
        result = CheckResult(**{**result.__dict__, "detail": f"{result.detail} (opcional)"})
    return [result]


async def _check_yahoo(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    snapshot = await YahooCollector(runtime).chart("^VIX", as_of=moment)

    def describe(snap: RawSnapshot) -> str:
        meta = snap.payload["chart"]["result"][0].get("meta") or {}
        return f"VIX intradía {meta.get('regularMarketPrice', '?')}"

    result = _result(runtime.config, "VIX intradía (respaldo)", snapshot, describe)
    if result.status == "error":
        result = CheckResult(
            **{**result.__dict__, "detail": f"{result.detail}; se usa el VIX diario de FRED"}
        )
    return [result]


async def _check_telegram(runtime: CollectorRuntime, moment: datetime) -> list[CheckResult]:
    snapshot = await TelegramCollector(runtime).get_me(as_of=moment)

    def describe(snap: RawSnapshot) -> str:
        return f"bot @{snap.payload['result'].get('username', '?')} responde"

    return [_result(runtime.config, "bot de alertas", snapshot, describe)]


CHECKS: list[Callable[[CollectorRuntime, datetime], Awaitable[list[CheckResult]]]] = [
    _check_alpaca,
    _check_finnhub,
    _check_fred,
    _check_sec,
    _check_rss,
    _check_fx,
    _check_massive,
    _check_yahoo,
    _check_telegram,
]


async def run_checks(
    runtime: CollectorRuntime, moment: datetime | None = None
) -> list[CheckResult]:
    moment = moment if moment is not None else clock.now()
    groups = await asyncio.gather(*(check(runtime, moment) for check in CHECKS))
    return [result for group in groups for result in group]


def render(results: list[CheckResult], moment: datetime) -> str:
    """The report as plain text, in Spanish."""
    rows = [
        (
            result.source,
            result.label,
            STATUS_LABELS[result.status],
            f"{result.latency_ms} ms" if result.latency_ms is not None else "—",
            "sí" if result.critical else "no",
            result.detail,
        )
        for result in results
    ]
    headers = ("Fuente", "Dato", "Estado", "Latencia", "Crítica", "Detalle")
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(5)]
    lines = [
        f"VÉRTICE · chequeo de fuentes · {clock.dual_label(moment)} · {clock.market_date(moment)}",
        "",
        "  ".join(header.ljust(widths[i]) if i < 5 else header for i, header in enumerate(headers)),
        "  ".join("-" * width for width in widths) + "  -------",
    ]
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) if i < 5 else cell for i, cell in enumerate(row))
        )

    critical = [r for r in results if r.critical]
    failing = sorted({r.source for r in critical if not r.healthy})
    working = sorted({r.source for r in critical} - set(failing))
    optional = sorted({r.source for r in results if not r.critical and not r.healthy})
    lines.append("")
    if failing:
        lines.append(f"PRECAUCIÓN: fuentes críticas con problema: {', '.join(failing)}.")
    else:
        lines.append(f"Fuentes críticas: todas responden ({', '.join(working)}).")
    if optional:
        lines.append(f"Opcionales sin servicio o sin configurar: {', '.join(optional)}.")
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.critical and not r.healthy for r in results) else 0


def _migrated_engine() -> Engine | None:
    """The database engine, or None when the schema is not ready to record results."""
    try:
        engine = make_engine()
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    except Exception:
        return None
    return engine if current == head_revision() else None


async def _main_async(store: bool) -> int:
    config = settings.get_config()
    secrets = settings.get_secrets()
    engine = _migrated_engine() if store else None
    if engine is not None:
        with session_scope(engine) as session:
            sync_configuration(session, secrets)
    runtime = build_runtime(config, secrets, engine=engine)
    moment = clock.now()
    try:
        results = await run_checks(runtime, moment)
    finally:
        await runtime.aclose()
    print(render(results, moment))
    if store and engine is None:
        print("\nNota: la base no está migrada; el resultado no se guardó (corre `make migrate`).")
    return exit_code(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collectors.check", description="Prueba cada fuente de datos de VÉRTICE."
    )
    parser.add_argument(
        "--no-store", action="store_true", help="no guardar el resultado en la base de datos"
    )
    args = parser.parse_args(argv)
    configure_logging(
        "ERROR", secret_values=settings.get_secrets().secret_values(), stream=sys.stderr
    )
    return asyncio.run(_main_async(store=not args.no_store))


if __name__ == "__main__":
    sys.exit(main())
