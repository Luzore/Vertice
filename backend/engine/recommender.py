"""Node 8 (first part): the daily plan with rule-based verdicts.

Turns scored candidates into a ranked list of ideas, each with a verdict
(COMPRAR, VIGILAR or NO_HOY), its plan and the result of every control. The plan
always lists at least five ideas; when nothing deserves a purchase it says so and
explains why. Forcing bad purchases to fill the list is forbidden.

This is the plan the system produces without the AI ("solo reglas"). From Phase 4
the AI ranks the finalists; these rules remain the fallback and the veto.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime

import pandas as pd

from core import clock
from core.settings import AppConfig
from engine import controls as ctl
from engine.data_engine import MarketState
from engine.indicators import IndicatorResult, Levels
from engine.portfolio import Portfolio
from engine.risk_profile import RiskProfile, profile_for
from engine.schemas import EconEvent
from engine.screener import Candidate, screen
from engine.trade_plan import TradePlan, build_trade_plan

WATCH_MARGIN = 10.0  # points below the minimum score that still earn a VIGILAR
MAX_REJECTED_LISTED = 5


@dataclass(frozen=True)
class Idea:
    ticker: str
    name: str
    sector_etf: str | None
    setup: str | None
    score: float
    breakdown: dict[str, float]
    verdict: str
    # Why it is not a COMPRAR; empty when it is.
    reason: str
    plan: TradePlan | None
    controls: tuple[ctl.ControlResult, ...]
    notes: tuple[str, ...] = ()
    shares: float = 0.0
    amount: float = 0.0
    risk_usd: float = 0.0
    size_multiplier: float = 1.0
    watch_trigger: str | None = None
    # True while the verdict comes from rules only.
    no_ai: bool = True
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DailyPlan:
    as_of: datetime
    plan_date: date
    regime: str
    regime_reasons: tuple[str, ...]
    market_reading: str
    events_today: tuple[EconEvent, ...]
    ideas: tuple[Idea, ...]
    # How many tickers were scored and how many had a buy setup.
    screened: int
    with_setup: int

    def by_verdict(self, verdict: str) -> list[Idea]:
        return [idea for idea in self.ideas if idea.verdict == verdict]


def correlation_60d(first: pd.DataFrame | None, second: pd.DataFrame | None) -> float:
    """Correlation of daily returns over the last 60 sessions both traded."""
    if first is None or second is None:
        return float("nan")
    joined = pd.concat([first["close"], second["close"]], axis=1, join="inner").tail(61)
    if len(joined) < 30:
        return float("nan")
    returns = joined.pct_change().dropna()
    return float(returns.iloc[:, 0].corr(returns.iloc[:, 1]))


def _watch_trigger(candidate: Candidate, plan: TradePlan | None) -> str | None:
    if plan is None:
        return None
    if candidate.setup == "RUPTURA":
        return (
            f"cierre de vela de 5 min arriba de ${plan.entry_max:,.2f} con volumen "
            "relativo mayor a 1.5"
        )
    return (
        f"precio dentro de la zona ${plan.entry_min:,.2f}–${plan.entry_max:,.2f} "
        f"sin pasar de ${plan.no_chase_above:,.2f}"
    )


def market_reading(indicators: IndicatorResult) -> str:
    """One paragraph, in Spanish, on the state of the market (rules only)."""
    context = indicators.context
    parts = [
        f"Régimen {indicators.regime.replace('_', ' ')}: {'; '.join(indicators.regime_reasons)}."
    ]
    vix, breadth = (
        context.get("vix_level", math.nan),
        context.get("breadth_pct_above_sma50", math.nan),
    )
    if not math.isnan(vix):
        parts.append(f"VIX en {vix:.1f}.")
    if not math.isnan(breadth):
        parts.append(f"{breadth:.0f}% de las acciones cotiza sobre su promedio de 50 días.")
    highs, lows = context.get("new_highs_52w", math.nan), context.get("new_lows_52w", math.nan)
    if not math.isnan(highs):
        parts.append(f"Nuevos máximos de 52 semanas: {highs:.0f}; nuevos mínimos: {lows:.0f}.")
    return " ".join(parts)


def _size(
    idea: Idea,
    portfolio: Portfolio,
    profile: RiskProfile,
    config: AppConfig,
    cash: float,
    sector_value: float,
) -> tuple[float, float, float]:
    """Shares, amount and dollars at risk for one idea (the basic rule; Phase 5
    adds conviction, streak and expectancy)."""
    plan = idea.plan
    capital = portfolio.capital
    risk_budget = capital * profile.risk_pct / 100 * idea.size_multiplier
    shares = risk_budget / plan.risk_per_share
    limits = [
        profile.max_position_pct / 100 * capital,
        cash,
        config.controls.max_sector_pct / 100 * capital - sector_value,
    ]
    shares = min(shares, max(0.0, min(limits)) / plan.entry_max)
    step = 0.01 if config.capital.fractional_shares else 1.0
    shares = math.floor(shares / step + 1e-9) * step
    return shares, shares * plan.entry_max, shares * plan.risk_per_share


def build_plan(
    state: MarketState,
    indicators: IndicatorResult,
    portfolio: Portfolio,
    config: AppConfig,
    *,
    candidates: Sequence[Candidate] | None = None,
    opening_prices: Mapping[str, float] | None = None,
) -> DailyPlan:
    """The plan of the day from rules. ``opening_prices`` is given at the 09:45
    recheck: an idea that opened above its no-chase price becomes VIGILAR."""
    profile = profile_for(config.system.aggressiveness)
    scored = list(candidates) if candidates is not None else screen(indicators, config.screener)
    with_setup = [c for c in scored if c.setup is not None]
    selected = with_setup[: config.screener.top_n_to_controls]
    minimum = config.plan.min_ideas_listed
    if len(selected) < minimum:
        # Not enough setups today: complete the list with the best of the rest, so
        # the plan can show what came closest and why it is not a purchase.
        rest = [c for c in scored if c.setup is None]
        selected = [*selected, *rest[: minimum - len(selected)]]

    opening_prices = opening_prices or {}
    news_by_ticker: dict[str, list] = {}
    for item in state.news:
        for ticker in item.tickers:
            news_by_ticker.setdefault(ticker, []).append(item)

    ideas: list[Idea] = []
    for candidate in selected:
        ticker = candidate.ticker
        row = state.universe.loc[ticker] if ticker in state.universe.index else None
        sector_etf = row["sector_etf"] if row is not None and pd.notna(row["sector_etf"]) else None
        market_cap = row["market_cap_usd"] if row is not None else None
        plan = build_trade_plan(
            candidate.metrics, indicators.levels.get(ticker) or Levels(), profile
        )
        context = ctl.ControlContext(
            as_of=state.as_of,
            config=config,
            profile=profile,
            regime=indicators.regime,
            candidate=candidate,
            plan=plan,
            portfolio=portfolio,
            quality=state.quality.get(ticker),
            quote=state.quotes.get(ticker),
            sector_etf=sector_etf,
            market_cap_usd=float(market_cap)
            if market_cap is not None and pd.notna(market_cap)
            else None,
            sic_listed=bool(row["sic_listed"]) if row is not None else False,
            econ_events=state.econ_events,
            news=news_by_ticker.get(ticker, ()),
            correlations={
                holding.ticker: correlation_60d(
                    state.daily.get(ticker), state.daily.get(holding.ticker)
                )
                for holding in portfolio.holdings
            },
            opening_price=opening_prices.get(ticker),
        )
        results = tuple(ctl.run_controls(context))
        verdict, reason, trigger = "COMPRAR", "", None
        if candidate.setup is None:
            verdict, reason = "NO_HOY", "sin setup de compra"
        elif ctl.is_blocked(results):
            verdict, reason = "NO_HOY", "; ".join(ctl.blocking_reasons(results))
        elif config.system.mode != "recommend":
            verdict, reason = "VIGILAR", "el sistema está en modo solo vigilar: no propone compras"
        elif candidate.score < profile.min_score - WATCH_MARGIN:
            verdict = "NO_HOY"
            reason = f"puntaje {candidate.score:.0f}; el mínimo es {profile.min_score:.0f}"
        elif candidate.score < profile.min_score:
            verdict, trigger = "VIGILAR", _watch_trigger(candidate, plan)
            reason = f"puntaje {candidate.score:.0f}, cerca del mínimo de {profile.min_score:.0f}"
        elif ticker in opening_prices and opening_prices[ticker] > plan.no_chase_above:
            verdict, trigger = "VIGILAR", _watch_trigger(candidate, plan)
            reason = (
                f"abrió en ${opening_prices[ticker]:,.2f}, arriba del precio de no perseguir "
                f"(${plan.no_chase_above:,.2f})"
            )
        ideas.append(
            Idea(
                ticker=ticker,
                name=str(row["name"]) if row is not None else ticker,
                sector_etf=sector_etf,
                setup=candidate.setup,
                score=candidate.score,
                breakdown=candidate.breakdown,
                verdict=verdict,
                reason=reason,
                plan=plan,
                controls=results,
                notes=tuple(candidate.notes),
                size_multiplier=ctl.size_multiplier(results),
                watch_trigger=trigger,
                metrics=candidate.metrics,
            )
        )

    # Give out cash in score order, within the limits of positions, cash and sector.
    cash = portfolio.cash
    slots = profile.max_positions - len(portfolio.holdings)
    sector_values: dict[str | None, float] = {}
    sized: list[Idea] = []
    for idea in sorted(ideas, key=lambda i: -i.score):
        if idea.verdict != "COMPRAR":
            sized.append(idea)
            continue
        if portfolio.holds(idea.ticker):
            sized.append(
                replace(
                    idea, verdict="VIGILAR", reason="ya tienes una posición abierta en este ticker"
                )
            )
            continue
        if slots <= 0:
            sized.append(
                replace(
                    idea,
                    verdict="VIGILAR",
                    reason=f"sin espacio: el límite es de {profile.max_positions} posiciones",
                    watch_trigger=_watch_trigger(_as_candidate(idea), idea.plan),
                )
            )
            continue
        held = portfolio.sector_value(idea.sector_etf) + sector_values.get(idea.sector_etf, 0.0)
        shares, amount, risk_usd = _size(
            idea, portfolio, profile, config, cash, held if idea.sector_etf else 0.0
        )
        if shares <= 0:
            step = 0.01 if config.capital.fractional_shares else 1.0
            allowed = portfolio.capital * profile.risk_pct / 100 * idea.size_multiplier
            if allowed < idea.plan.risk_per_share * step:
                # The account is too small for even one share at this risk per idea.
                why = (
                    f"el riesgo permitido por idea (${allowed:,.2f}) no alcanza para una acción "
                    f"(riesgo por acción ${idea.plan.risk_per_share:,.2f})"
                )
            else:
                why = "COMPRAR si liberas efectivo"
            sized.append(replace(idea, verdict="VIGILAR", reason=why))
            continue
        cash -= amount
        slots -= 1
        sector_values[idea.sector_etf] = sector_values.get(idea.sector_etf, 0.0) + amount
        sized.append(replace(idea, shares=shares, amount=amount, risk_usd=risk_usd))

    order = {"COMPRAR": 0, "VIGILAR": 1, "NO_HOY": 2}
    ranked = sorted(sized, key=lambda i: (order[i.verdict], -i.score, i.ticker))
    actionable = [i for i in ranked if i.verdict != "NO_HOY"]
    rejected = [i for i in ranked if i.verdict == "NO_HOY"]
    keep = max(minimum - len(actionable), min(MAX_REJECTED_LISTED, len(rejected)))
    today = clock.market_date(state.as_of)
    return DailyPlan(
        as_of=state.as_of,
        plan_date=today,
        regime=indicators.regime,
        regime_reasons=tuple(indicators.regime_reasons),
        market_reading=market_reading(indicators),
        events_today=tuple(
            event for event in state.econ_events if clock.market_date(event.scheduled_at) == today
        ),
        ideas=tuple([*actionable, *rejected[:keep]]),
        screened=len(scored),
        with_setup=len(with_setup),
    )


def _as_candidate(idea: Idea) -> Candidate:
    return Candidate(
        idea.ticker, idea.setup, idea.score, idea.breakdown, list(idea.notes), idea.metrics
    )


_VERDICT_LABELS = {"COMPRAR": "COMPRAR", "VIGILAR": "VIGILAR", "NO_HOY": "NO HOY"}
_WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def render_plan(plan: DailyPlan, config: AppConfig, capital: float) -> str:
    """The plan as plain text in Spanish (what Telegram and the report show)."""
    day = plan.plan_date
    header = (
        f"{config.system.name} · Plan del día · {_WEEKDAYS[day.weekday()]} {day.day} "
        f"{_MONTHS[day.month - 1]} {day.year} · Régimen {plan.regime.replace('_', ' ')}"
    )
    lines = [header, plan.market_reading]
    if plan.events_today:
        events = ", ".join(
            f"{e.name} {clock.to_market(e.scheduled_at):%H:%M} NY" for e in plan.events_today
        )
        lines.append(f"Eventos de hoy: {events}.")
    buys = plan.by_verdict("COMPRAR")
    if not buys:
        lines.append("Hoy no hay ninguna idea en COMPRAR: ninguna supera todos los controles.")
    for idea in plan.ideas:
        lines.append("")
        label = _VERDICT_LABELS[idea.verdict]
        setup = idea.setup or "sin setup"
        suffix = " · sin IA" if idea.no_ai else ""
        lines.append(f"{label}  {idea.ticker}  ({setup} · puntaje {idea.score:.0f}{suffix})")
        p = idea.plan
        if idea.verdict != "NO_HOY" and p is not None:
            lines.append(
                f"Entrada: límite {p.entry_min:,.2f}–{p.entry_max:,.2f} "
                f"(no perseguir sobre {p.no_chase_above:,.2f})"
            )
            lines.append(
                f"Stop: {p.stop:,.2f} (−1R)   Objetivos: {p.targets[0]:,.2f} "
                f"(+{p.r_multiple(p.targets[0]):.1f}R) / {p.targets[1]:,.2f} "
                f"(+{p.r_multiple(p.targets[1]):.1f}R)"
            )
        if idea.verdict == "COMPRAR":
            share = 100 * idea.amount / capital if capital > 0 else 0.0
            lines.append(
                f"Tamaño: {idea.shares:g} acciones ≈ ${idea.amount:,.0f} "
                f"({share:.0f}% del capital) "
                f"· riesgo ${idea.risk_usd:,.0f}"
            )
        if idea.notes:
            lines.append(f"Por qué: {'; '.join(idea.notes)}")
        if idea.reason:
            lines.append(f"{'Motivo' if idea.verdict == 'NO_HOY' else 'Pendiente'}: {idea.reason}")
        if idea.watch_trigger:
            lines.append(f"Se activa si: {idea.watch_trigger}")
        warnings = [f"{c.id} {c.reason}" for c in idea.controls if c.effect == "reduce"]
        if warnings:
            lines.append(f"Avisos: {'; '.join(warnings)}")
    return "\n".join(lines)
