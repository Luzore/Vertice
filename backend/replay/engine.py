"""Replay: run the whole pipeline over historical days with a simulated clock.

For each trading day the plan is built exactly as it would have been at
publication time, using only what was known then (principle 15). Every COMPRAR
idea is then followed as a shadow position with the exit rules, on the bars that
came afterwards, and the result is compared with holding SPY.

Two parts are kept apart on purpose:

* **deciding**: gets a point-in-time market state and never sees a later bar;
* **simulating**: looks up what happened next, only to fill and close positions.

What a replay can and cannot tell is spelled out in the report. Above all, the
universe is today's list of liquid companies, which flatters past results.
"""

from __future__ import annotations

import multiprocessing
import time as wall_clock
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import Engine

from core import clock, market_calendar
from core.settings import AppConfig
from db.session import session_scope
from engine import exits
from engine.ai_analyst import Analyst, simulated_analyst
from engine.data_engine import MarketDataCache, Quality
from engine.exits import Bar, ShadowPosition
from engine.indicators import IndicatorResult, Levels, compute_indicators
from engine.portfolio import Holding, Portfolio
from engine.recommender import DailyPlan, Idea, build_plan
from engine.risk_profile import profile_for
from engine.screener import Candidate, screen

EXTRA_CANDIDATES = 15
DEFAULT_HORIZON_DAYS = 10


@dataclass(frozen=True)
class DaySignals:
    """What the screener saw on one day: enough to build that day's plan."""

    day: date
    as_of: datetime
    regime: str
    regime_reasons: tuple[str, ...]
    context: dict[str, float]
    candidates: tuple[Candidate, ...]
    levels: dict[str, Levels]
    quality: dict[str, Quality]
    screened: int
    with_setup: int
    seconds: float


@dataclass(frozen=True)
class IdeaRecord:
    day: date
    ticker: str
    verdict: str
    setup: str | None
    score: float
    reason: str
    # What happened to a COMPRAR idea at the open: entered, not fillable, or moved to VIGILAR.
    outcome: str = ""


@dataclass(frozen=True)
class DayRecord:
    day: date
    regime: str
    listed: int
    buy: int
    watch: int
    rejected: int
    entered: int
    open_positions: int
    cash: float
    equity: float
    spy_equity: float
    seconds: float


@dataclass
class ReplayResult:
    start: date
    end: date
    capital: float
    days: list[DayRecord] = field(default_factory=list)
    ideas: list[IdeaRecord] = field(default_factory=list)
    trades: list[ShadowPosition] = field(default_factory=list)
    blockers: Counter = field(default_factory=Counter)
    plans: dict[date, DailyPlan] = field(default_factory=dict)
    load_seconds: float = 0.0
    # "none" (rule verdicts) or "simulated" (the simulated AI ranks the finalists).
    ai: str = "none"
    # Days on which the AI answered, and calls it took.
    ai_days: int = 0
    ai_calls: int = 0

    @property
    def seconds_per_day(self) -> float:
        return sum(d.seconds for d in self.days) / len(self.days) if self.days else 0.0

    @property
    def slowest_day_seconds(self) -> float:
        return max((d.seconds for d in self.days), default=0.0)


# The data is loaded once and inherited by the worker processes (fork), so it is
# not copied or sent between processes.
_DATA: MarketDataCache | None = None
_CONFIG: AppConfig | None = None


def day_signals(data: MarketDataCache, config: AppConfig, day: date) -> DaySignals:
    """Indicators and screening for one day, as of that day's plan publication time."""
    started = wall_clock.perf_counter()
    as_of = clock.market_time(day, config.plan.publish_time_et)
    with clock.simulated(as_of):
        state = data.state_at(as_of)
        indicators = compute_indicators(state, config.regime)
        scored = screen(indicators, config.screener)
    with_setup = [c for c in scored if c.setup is not None]
    without = [c for c in scored if c.setup is None]
    kept = [
        *with_setup[: config.screener.top_n_to_controls + EXTRA_CANDIDATES],
        *without[: config.plan.min_ideas_listed],
    ]
    return DaySignals(
        day=day,
        as_of=as_of,
        regime=indicators.regime,
        regime_reasons=tuple(indicators.regime_reasons),
        context=indicators.context,
        candidates=tuple(kept),
        levels={c.ticker: indicators.levels.get(c.ticker, Levels()) for c in kept},
        quality={c.ticker: state.quality[c.ticker] for c in kept if c.ticker in state.quality},
        screened=len(scored),
        with_setup=len(with_setup),
        seconds=wall_clock.perf_counter() - started,
    )


def _signals_worker(day: date) -> DaySignals:
    return day_signals(_DATA, _CONFIG, day)


def _bar(data: MarketDataCache, ticker: str, day: date) -> Bar | None:
    row = data.bar_on(ticker, day)
    if row is None:
        return None
    return Bar(day, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))


def _earnings_before_next_open(
    data: MarketDataCache, ticker: str, day: date, as_of: datetime
) -> bool:
    """True when the company reports today after the close or before tomorrow's open."""
    return exits.reports_before_next_open(
        data.events_for(ticker, as_of), day, market_calendar.next_trading_day(day)
    )


class _Account:
    """The shadow account: cash plus the virtual positions that follow the plan."""

    def __init__(self, capital: float) -> None:
        self.cash = capital
        self.positions: list[ShadowPosition] = []
        self.closed: list[ShadowPosition] = []
        self.stopped_out: dict[str, date] = {}
        self.equity_history: list[tuple[date, float]] = []

    def equity(self) -> float:
        return self.cash + sum(p.shares_open * p.last_price for p in self.positions)

    def week_pnl_pct(self, day: date) -> float:
        """Result since the last close of the previous week, in percent."""
        monday = day - timedelta(days=day.weekday())
        before = [value for when, value in self.equity_history if when < monday]
        current = self.equity_history[-1][1] if self.equity_history else self.cash
        return (current / before[-1] - 1) * 100 if before and before[-1] > 0 else 0.0

    def snapshot(self, day: date) -> Portfolio:
        return Portfolio(
            cash=self.cash,
            holdings=tuple(
                Holding(
                    p.ticker,
                    p.sector_etf,
                    p.shares_open,
                    p.entry_price,
                    p.last_price,
                    p.stop,
                    p.entry_day,
                )
                for p in self.positions
            ),
            stopped_out=dict(self.stopped_out),
            day_pnl_pct=0.0,  # the plan is published before the open: no result yet today
            week_pnl_pct=self.week_pnl_pct(day),
        )

    def settle(self, fills: list[exits.Fill]) -> None:
        for fill in fills:
            if fill.side == "sell":
                self.cash += fill.price * fill.shares - fill.cost
            else:
                self.cash -= fill.price * fill.shares + fill.cost

    def retire_closed(self) -> None:
        for position in [p for p in self.positions if not p.is_open]:
            if position.exit_reason in (exits.STOP, exits.STOP_GAP):
                self.stopped_out[position.ticker] = position.exit_day
            self.closed.append(position)
        self.positions = [p for p in self.positions if p.is_open]


def _enter(
    idea: Idea, bar: Bar, account: _Account, config: AppConfig, regime: str
) -> tuple[ShadowPosition | None, str]:
    """Simulate the limit order of a COMPRAR idea on the day's bar."""
    plan = idea.plan
    price, why = exits.theoretical_entry(plan.entry_max, plan.stop, bar)
    if price is None:
        return None, why
    shares = idea.shares
    cost = exits.trading_cost(price, shares, config.costs)
    if price * shares + cost > account.cash:
        step = 0.01 if config.capital.fractional_shares else 1.0
        affordable = account.cash / (price * (1 + cost / (price * shares)))
        shares = int(affordable / step) * step
    if shares <= 0:
        return None, "no ejecutable: sin efectivo"
    position = ShadowPosition(
        ticker=idea.ticker,
        setup=idea.setup,
        sector_etf=idea.sector_etf,
        entry_day=bar.day,
        entry_price=price,
        shares=shares,
        stop=plan.stop,
        targets=plan.targets,
        atr=plan.atr,
        horizon_days=DEFAULT_HORIZON_DAYS,
        regime_at_entry=regime,
        score=idea.score,
    )
    account.settle([exits.open_position(position, config.costs)])
    return position, "entró"


def simulate(
    data: MarketDataCache,
    config: AppConfig,
    signals: list[DaySignals],
    *,
    capital: float,
    analyst: Analyst | None = None,
) -> ReplayResult:
    """The sequential part: plans, entries, exits and the account, day by day.

    ``analyst`` is only ever the simulated one: a real AI already knows what the
    market did on past dates, so a replay with it would measure nothing.
    """
    result = ReplayResult(start=signals[0].day, end=signals[-1].day, capital=capital)
    result.ai = "simulated" if analyst is not None else "none"
    account = _Account(capital)
    profile = profile_for(config.system.aggressiveness)
    spy_base: float | None = None

    for today in signals:
        started = wall_clock.perf_counter()
        day = today.day
        tickers = {c.ticker for c in today.candidates} | {p.ticker for p in account.positions}
        with clock.simulated(today.as_of):
            state = data.state_at(today.as_of, tickers)
            # The state is cut to the candidates; quality was measured on the full day.
            indicators = IndicatorResult(
                as_of=today.as_of,
                metrics=pd.DataFrame(),
                context=today.context,
                regime=today.regime,
                regime_reasons=list(today.regime_reasons),
                levels=today.levels,
            )
            portfolio = account.snapshot(day)
            published = build_plan(
                state,
                indicators,
                portfolio,
                config,
                candidates=today.candidates,
                analyst=analyst,
            )
        result.plans[day] = published
        result.ai_days += published.used_ai

        # 09:45 recheck with the opening prices of the candidates.
        bars = {t: bar for t in tickers if (bar := _bar(data, t, day)) is not None}
        recheck_time = clock.market_time(day, config.plan.recheck_time_et)
        with clock.simulated(recheck_time):
            rechecked = build_plan(
                state,
                indicators,
                portfolio,
                config,
                candidates=today.candidates,
                opening_prices={t: bars[t].open for t in bars},
                # The recheck reuses the morning's review: the AI is not asked again.
                review=published.review,
            )
        final = {idea.ticker: idea for idea in rechecked.ideas}

        entered = 0
        new_positions: list[ShadowPosition] = []
        for idea in published.ideas:
            outcome = ""
            if idea.verdict == "COMPRAR":
                after = final.get(idea.ticker)
                if after is None or after.verdict != "COMPRAR":
                    outcome = (
                        f"no entró: {after.reason if after else 'salió del plan en la apertura'}"
                    )
                elif idea.ticker not in bars:
                    outcome = "no ejecutable: sin precio ese día"
                elif len(account.positions) + len(new_positions) >= profile.max_positions:
                    outcome = "no entró: límite de posiciones"
                else:
                    position, outcome = _enter(
                        after, bars[idea.ticker], account, config, today.regime
                    )
                    if position is not None:
                        new_positions.append(position)
                        entered += 1
            elif idea.verdict == "NO_HOY":
                for control in idea.controls:
                    if control.effect == "block":
                        result.blockers[f"{control.id} {control.name}"] += 1
            result.ideas.append(
                IdeaRecord(
                    day, idea.ticker, idea.verdict, idea.setup, idea.score, idea.reason, outcome
                )
            )

        # Manage what was already open, then the positions opened today (stop only).
        for position in account.positions:
            bar = bars.get(position.ticker)
            if bar is None:
                continue
            fills = exits.apply_bar(
                position,
                bar,
                config.exits,
                config.costs,
                earnings_before_next_open=_earnings_before_next_open(
                    data, position.ticker, day, today.as_of
                ),
                regime=today.regime,
                fractional_shares=config.capital.fractional_shares,
            )
            account.settle(fills)
        for position in new_positions:
            fills = exits.apply_bar(
                position, bars[position.ticker], config.exits, config.costs, entry_day=True
            )
            account.settle(fills)
        account.positions.extend(new_positions)
        account.retire_closed()

        equity = account.equity()
        account.equity_history.append((day, equity))
        spy = data.bar_on("SPY", day)
        if spy_base is None:
            previous = data.bars_until("SPY", market_calendar.previous_trading_day(day))
            spy_base = float(previous["close"].iloc[-1]) if previous is not None else None
        spy_equity = (
            capital * float(spy["close"]) / spy_base
            if spy is not None and spy_base
            else float("nan")
        )
        verdicts = Counter(idea.verdict for idea in published.ideas)
        result.days.append(
            DayRecord(
                day=day,
                regime=today.regime,
                listed=len(published.ideas),
                buy=verdicts["COMPRAR"],
                watch=verdicts["VIGILAR"],
                rejected=verdicts["NO_HOY"],
                entered=entered,
                open_positions=len(account.positions),
                cash=account.cash,
                equity=equity,
                spy_equity=spy_equity,
                seconds=today.seconds + wall_clock.perf_counter() - started,
            )
        )

    # Close what is still open at the last close, so every trade has a result.
    last_day = signals[-1].day
    for position in account.positions:
        bar = _bar(data, position.ticker, last_day)
        if bar is not None:
            fill = exits.close_at(position, bar, exits.END_OF_TEST, config.costs)
            if fill is not None:
                account.settle([fill])
    account.retire_closed()
    result.trades = account.closed
    if analyst is not None:
        result.ai_calls = analyst.gateway.calls_made
    return result


def run_replay(
    engine: Engine,
    config: AppConfig,
    start: date,
    end: date,
    *,
    capital: float | None = None,
    workers: int = 1,
    ai: str = "none",
) -> ReplayResult:
    """Replay every trading day from ``start`` to ``end``. ``ai`` is "none" for rule
    verdicts or "simulated" to run the finalists through the simulated AI."""
    if ai not in ("none", "simulated"):
        raise ValueError("el replay solo acepta ai='none' o ai='simulated'")
    global _DATA, _CONFIG
    days = market_calendar.trading_days_between(start, end)
    if not days:
        raise ValueError(f"no hay días hábiles entre {start} y {end}")
    loading = wall_clock.perf_counter()
    with session_scope(engine) as session:
        data = MarketDataCache(session, config, start=days[0], end=days[-1])
    load_seconds = wall_clock.perf_counter() - loading

    if workers > 1 and len(days) > 1:
        _DATA, _CONFIG = data, config
        try:
            with multiprocessing.get_context("fork").Pool(min(workers, len(days))) as pool:
                signals = pool.map(
                    _signals_worker, days, chunksize=max(1, len(days) // (workers * 4))
                )
        finally:
            _DATA = _CONFIG = None
    else:
        signals = [day_signals(data, config, day) for day in days]

    result = simulate(
        data,
        config,
        signals,
        capital=capital or config.capital.cash_usd,
        analyst=simulated_analyst(config) if ai == "simulated" else None,
    )
    result.load_seconds = load_seconds
    return result
