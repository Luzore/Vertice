"""The three daily runs around the plan (ROADMAP sections 11 and 16).

* **premarket** builds the Plan del día from the data already collected, with the
  real account read from the database, and saves it.
* **recheck** is the 09:45 review: purchases that opened too high stop being one.
* **close** settles the day: unanswered ideas become shadows, shadows are followed
  with the exit rules and the day's capital is recorded.

``python -m engine.daily premarket|recheck|close`` runs them by hand. The scheduler
that runs them on time arrives with the watcher.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from sqlalchemy import Engine, select

from core import clock, market_calendar, settings
from core.logging import configure_logging, get_logger
from core.settings import AppConfig
from db import models
from db.session import make_engine, session_scope
from engine import account, confirmations, exits, plan_store, risk, states
from engine.ai_analyst import Analyst, live_analyst, simulated_analyst
from engine.data_engine import build_market_state, load_corporate_events
from engine.indicators import compute_indicators
from engine.news_ai import refine_recent_news
from engine.recommender import DailyPlan, build_plan, render_plan
from engine.screener import screen

log = get_logger(__name__)


class NoMarket(Exception):
    """NYSE does not trade that day: there is no plan to build."""


@dataclass(frozen=True)
class CloseSummary:
    day: date
    # Tickers by what happened to their idea: expired, shadow, not_fillable.
    ideas: dict[str, list[str]]
    # Shadow ticker -> reasons of the sales the exit rules made today.
    shadow_sales: dict[str, list[str]]
    equity: float
    day_pnl: float | None
    drawdown_pct: float
    note: str = ""


def _trading_day(moment: datetime) -> date:
    day = clock.market_date(moment)
    if not market_calendar.is_trading_day(day):
        raise NoMarket(f"NYSE no abre el {day.isoformat()}: no hay Plan del día")
    return day


def run_premarket(
    engine: Engine,
    config: AppConfig,
    *,
    analyst: Analyst | None = None,
    as_of: datetime | None = None,
) -> tuple[int, DailyPlan]:
    """Build and save the plan of the day. Returns the run id and the plan."""
    started = clock.now()
    as_of = clock.ensure_utc(as_of) if as_of is not None else clock.now()
    _trading_day(as_of)
    with states.guarded_session(engine) as session:
        state = build_market_state(session, config, as_of=as_of)
        portfolio = account.load_portfolio(session, config, as_of=as_of)
        history = account.load_history(session)
        brake = account.current_brake(session, config, portfolio, history)
    indicators = compute_indicators(state, config.regime)
    scored = screen(indicators, config.screener)

    if analyst is not None:
        # The AI refines the news of the candidates before the controls read them.
        tickers = [c.ticker for c in scored if c.setup is not None]
        tickers = tickers[: config.screener.top_n_to_controls]
        with session_scope(engine) as session:
            refined, note = refine_recent_news(
                session,
                analyst.gateway,
                tickers,
                since=as_of - timedelta(hours=config.controls.severe_news_hours),
                until=as_of,
            )
        if note:
            log.warning("news_not_refined", reason=note)
        if refined:
            state = replace(state, news=[refined.get(item.url, item) for item in state.news])

    plan = build_plan(
        state,
        indicators,
        portfolio,
        config,
        candidates=scored,
        analyst=analyst,
        history=history,
        brake=brake,
    )
    with states.guarded_session(engine) as session:
        run_id = plan_store.save_plan(session, plan, started_at=started)
    log.info(
        "plan_saved",
        run_id=run_id,
        plan_date=plan.plan_date.isoformat(),
        buy=len(plan.by_verdict("COMPRAR")),
        listed=len(plan.ideas),
        ai=plan.review.status if plan.review is not None else "not_used",
    )
    return run_id, plan


def _bars_of(session, day: date, tickers: set[str]) -> dict[str, exits.Bar]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(models.BarDaily).where(
            models.BarDaily.session_date == day, models.BarDaily.ticker.in_(sorted(tickers))
        )
    )
    return {
        row.ticker: exits.Bar(
            day, float(row.open), float(row.high), float(row.low), float(row.close)
        )
        for row in rows
    }


def run_recheck(
    engine: Engine,
    config: AppConfig,
    *,
    opening_prices: dict[str, float] | None = None,
    day: date | None = None,
) -> list[tuple[str, str, str]]:
    """The review after the open. Without ``opening_prices`` the opens of the day's
    bars already stored are used. Returns the ideas that changed."""
    day = day or clock.market_date(clock.now())
    with states.guarded_session(engine) as session:
        if opening_prices is None:
            tickers = {idea.ticker for idea in plan_store.todays_ideas(session, day)}
            opening_prices = {t: bar.open for t, bar in _bars_of(session, day, tickers).items()}
        return plan_store.recheck_open(session, config, day, opening_prices)


def run_close(engine: Engine, config: AppConfig, *, day: date | None = None) -> CloseSummary:
    """Settle ``day``: shadows, exit rules on the shadows and the capital of the day."""
    day = day or clock.market_date(clock.now())
    next_day = market_calendar.next_trading_day(day)
    with states.guarded_session(engine) as session:
        open_shadows = session.scalars(
            select(models.Position.ticker).where(
                models.Position.is_shadow.is_(True), models.Position.state != "CLOSED"
            )
        ).all()
        real = account.real_positions(session, open_only=True)
        tickers = (
            {idea.ticker for idea in plan_store.todays_ideas(session, day)}
            | set(open_shadows)
            | {p.ticker for p in real}
        )
        bars = _bars_of(session, day, tickers | {"SPY"})
        note = ""
        if tickers and not (set(bars) - {"SPY"}):
            note = f"faltan las barras del {day.isoformat()}: descarga los datos y repite el cierre"
            return CloseSummary(day, {}, {}, float("nan"), None, 0.0, note)

        trading = market_calendar.get_session(day)
        close_time = trading.close if trading is not None else clock.market_time(day, time(16))
        events = load_corporate_events(session, close_time, tickers)
        run_id = plan_store.plan_run_id(session, day)
        regime = session.get(models.Run, run_id).regime if run_id is not None else ""
        shadow_sales = confirmations.update_shadows(
            session,
            config,
            day,
            bars,
            regime=regime or "",
            reports_before_next_open=lambda ticker: exits.reports_before_next_open(
                events.get(ticker, ()), day, next_day
            ),
        )
        ideas = confirmations.settle_day(session, config, day, bars)

        prices = {ticker: bar.close for ticker, bar in bars.items()}
        portfolio = account.load_portfolio(session, config, as_of=close_time, prices=prices)
        history = account.load_history(session)
        equity = portfolio.capital
        previous = account.equity_before(session, day)
        fall = risk.drawdown_pct(history, equity)
        row = session.get(models.MetricsDaily, day) or models.MetricsDaily(day=day)
        row.equity_real = equity
        row.spy_close = bars["SPY"].close if "SPY" in bars else None
        row.pnl = equity - previous if previous is not None else None
        row.drawdown = fall
        session.add(row)
        session.flush()
        # A loss that trips the weekly brake or the pause is remembered from tonight.
        after = account.load_portfolio(session, config, as_of=close_time, prices=prices)
        account.current_brake(session, config, after, account.load_history(session))
    return CloseSummary(day, ideas, shadow_sales, equity, row.pnl, fall, note)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.daily",
        description="Corridas diarias: plan premercado, revisión tras la apertura y cierre.",
    )
    parser.add_argument("run", choices=("premarket", "recheck", "close"))
    parser.add_argument(
        "--ai",
        choices=("none", "simulated", "real"),
        default="real",
        help="quién analiza a los finalistas en 'premarket' (por defecto, la IA real)",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        metavar="AAAA-MM-DD",
        help="día del cierre o de la revisión (por defecto, hoy)",
    )
    args = parser.parse_args(argv)
    config = settings.get_config()
    configure_logging(
        "WARNING", secret_values=settings.get_secrets().secret_values(), stream=sys.stderr
    )
    engine = make_engine()
    try:
        if args.run == "premarket":
            analyst = None
            if args.ai == "simulated":
                analyst = simulated_analyst(config)
            elif args.ai == "real":
                analyst = live_analyst(config, engine)
            _, plan = run_premarket(engine, config, analyst=analyst)
            with session_scope(engine) as session:
                capital = account.load_portfolio(session, config).capital
            print(render_plan(plan, config, capital))
        elif args.run == "recheck":
            changed = run_recheck(engine, config, day=args.date)
            for ticker, verdict, reason in changed:
                print(f"{ticker}: pasa a {verdict.replace('_', ' ')} ({reason})")
            if not changed:
                print("Sin cambios tras la apertura.")
        else:
            summary = run_close(engine, config, day=args.date)
            if summary.note:
                print(summary.note)
                return 1
            print(f"Cierre del {summary.day.isoformat()}: capital ${summary.equity:,.2f}")
            for label, key in (
                ("Sombras nuevas", "shadow"),
                ("No ejecutables", "not_fillable"),
                ("Sin respuesta", "expired"),
            ):
                print(f"{label}: {', '.join(summary.ideas.get(key, [])) or 'ninguna'}")
            for ticker, reasons in summary.shadow_sales.items():
                print(f"Sombra {ticker}: {'; '.join(reasons)}")
    except (NoMarket, plan_store.PlanLocked) as problem:
        print(problem)
        return 1
    except states.InvalidTransition as problem:
        print(f"PRECAUCIÓN: {problem}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
