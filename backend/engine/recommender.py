"""Node 8 (first part): the daily plan with rule-based verdicts.

Turns scored candidates into a ranked list of ideas, each with a verdict
(COMPRAR, VIGILAR or NO_HOY), its plan and the result of every control. The plan
always lists at least five ideas; when nothing deserves a purchase it says so and
explains why. Forcing bad purchases to fill the list is forbidden.

Without the AI the verdicts come from rules ("solo reglas"). With it, the
candidates that pass the blocking controls (the finalists) are sent to the analyst,
and its validated decisions set the verdict, under three limits that never move:

* the controls run again on the AI's levels and still veto (principle 5);
* COMPRAR needs the minimum score and the minimum probability of the risk profile;
* the size of the position is computed by the risk engine, never by the AI
  (principle 4), and an idea is only bought if its expectancy after costs is
  at least +0.15R.

If the AI is off, out of quota or failing, the plan falls back to the rules and
every idea is marked "sin IA".
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime

import pandas as pd

from core import clock
from core.settings import AppConfig
from engine import controls as ctl
from engine import risk
from engine.ai_analyst import Analyst, AnalystReview
from engine.ai_validation import AIDecision
from engine.data_engine import MarketState
from engine.dossier import Finalist
from engine.indicators import IndicatorResult, Levels
from engine.levels import valid_levels
from engine.portfolio import Portfolio
from engine.risk import (
    AccountHistory,
    Brake,
    Expectancy,
    Rotation,
    conviction_multiplier,
    expectancy,
    position_size,
    propose_rotations,
    streak_multiplier,
    worst_case_risk,
)
from engine.risk_profile import RiskProfile, profile_for
from engine.schemas import EconEvent
from engine.screener import Candidate, screen
from engine.trade_plan import TradePlan, build_trade_plan

WATCH_MARGIN = 10.0  # points below the minimum score that still earn a VIGILAR
MIN_EV_R = risk.MIN_EV_R
MAX_REJECTED_LISTED = 5
# Controls that only say "there is no room or no cash": an idea blocked by nothing
# else can still be worth a rotation.
ROOM_CONTROLS = frozenset({"C15", "C18"})


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
    # What the AI said about this idea, once validated; None when it was not asked.
    ai: AIDecision | None = None
    # Expected value after costs, when the idea has a plan and no control blocked it.
    expectancy: Expectancy | None = None
    # Factors applied to the base risk (conviction, controls, streak) and what capped the size.
    multipliers: dict[str, float] = field(default_factory=dict)
    limited_by: str | None = None

    @property
    def ev_r(self) -> float | None:
        return self.expectancy.ev_r if self.expectancy is not None else None

    @property
    def probability(self) -> float | None:
        """The probability the size and the expectancy used; None without the AI."""
        if self.expectancy is None or self.expectancy.assumed_probability:
            return None
        return self.expectancy.probability


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
    # The analyst's review of the finalists; None when no analyst took part.
    review: AnalystReview | None = None
    # Proposals to swap a weak position for a better idea; the user decides.
    rotations: tuple[Rotation, ...] = ()
    # The automatic brake in force, and why the size is halved by a bad run.
    brake: Brake | None = None
    streak_note: str = ""

    @property
    def used_ai(self) -> bool:
        return self.review is not None and self.review.used_ai

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


@dataclass(frozen=True)
class _Evaluated:
    """A candidate with its rule-based plan and the result of the controls on it."""

    candidate: Candidate
    name: str
    sector_etf: str | None
    context: ctl.ControlContext
    results: tuple[ctl.ControlResult, ...]

    @property
    def is_finalist(self) -> bool:
        return self.candidate.setup is not None and not ctl.is_blocked(self.results)


def _rule_verdict(
    candidate: Candidate,
    plan: TradePlan | None,
    profile: RiskProfile,
    config: AppConfig,
    opening_price: float | None,
) -> tuple[str, str, str | None]:
    """Verdict, reason and watch trigger of a candidate no control blocked."""
    if config.system.mode != "recommend":
        return "VIGILAR", "el sistema está en modo solo vigilar: no propone compras", None
    if candidate.score < profile.min_score - WATCH_MARGIN:
        reason = f"puntaje {candidate.score:.0f}; el mínimo es {profile.min_score:.0f}"
        return "NO_HOY", reason, None
    if candidate.score < profile.min_score:
        reason = f"puntaje {candidate.score:.0f}, cerca del mínimo de {profile.min_score:.0f}"
        return "VIGILAR", reason, _watch_trigger(candidate, plan)
    if opening_price is not None and plan is not None and opening_price > plan.no_chase_above:
        reason = (
            f"abrió en ${opening_price:,.2f}, arriba del precio de no perseguir "
            f"(${plan.no_chase_above:,.2f})"
        )
        return "VIGILAR", reason, _watch_trigger(candidate, plan)
    return "COMPRAR", "", None


def _ai_discard_reason(decision: AIDecision) -> str:
    if decision.discard_reason:
        return decision.discard_reason
    why = decision.risks[0] if decision.risks else decision.thesis
    return f"la IA lo descartó: {why}" if why else "la IA lo descartó"


def build_plan(
    state: MarketState,
    indicators: IndicatorResult,
    portfolio: Portfolio,
    config: AppConfig,
    *,
    candidates: Sequence[Candidate] | None = None,
    opening_prices: Mapping[str, float] | None = None,
    analyst: Analyst | None = None,
    review: AnalystReview | None = None,
    history: AccountHistory | None = None,
    brake: Brake | None = None,
    calibrate: Callable[[float], float] | None = None,
) -> DailyPlan:
    """The plan of the day.

    With ``analyst`` the finalists are sent to the AI; ``review`` reuses a review
    already made (the 09:45 recheck does not ask again). With neither, the verdicts
    come from rules. ``opening_prices`` is given at the recheck: an idea that
    opened above its no-chase price becomes VIGILAR.

    ``history`` feeds the streak multiplier, ``brake`` is the automatic brake in
    force (no purchases while it is on) and ``calibrate`` turns the AI's raw
    probability into the calibrated one (the identity until there is evidence).
    """
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

    # First the rules: plan and controls of every candidate.
    evaluated: list[_Evaluated] = []
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
        evaluated.append(
            _Evaluated(
                candidate=candidate,
                name=str(row["name"]) if row is not None else ticker,
                sector_etf=sector_etf,
                context=context,
                results=tuple(ctl.run_controls(context)),
            )
        )

    # Then the AI, on the candidates that no control blocked.
    if review is None and analyst is not None:
        finalists = []
        for item in [e for e in evaluated if e.is_finalist][: config.screener.max_finalists]:
            ticker = item.candidate.ticker
            valid = valid_levels(
                item.candidate.metrics,
                indicators.levels.get(ticker) or Levels(),
                config.controls,
                item.context.plan,
            )
            if valid is None:
                continue
            finalists.append(
                Finalist(
                    candidate=item.candidate,
                    name=item.name,
                    sector_etf=item.sector_etf,
                    plan=item.context.plan,
                    valid=valid,
                    controls=item.results,
                    news=tuple(item.context.news),
                    events=tuple(state.corporate_events.get(ticker, ())),
                )
            )
        review = analyst.review(state, indicators, portfolio, profile, config, finalists)
    decisions = review.decisions if review is not None and review.used_ai else None

    ideas: list[Idea] = []
    no_room: list[tuple[str, float]] = []
    for item in evaluated:
        candidate, ticker = item.candidate, item.candidate.ticker
        plan, results = item.context.plan, item.results
        opening = opening_prices.get(ticker)
        decision = decisions.get(ticker) if decisions is not None else None
        trigger: str | None = None
        if candidate.setup is None:
            verdict, reason = "NO_HOY", "sin setup de compra"
        elif ctl.is_blocked(results):
            verdict, reason = "NO_HOY", "; ".join(ctl.blocking_reasons(results))
        elif decisions is None:
            verdict, reason, trigger = _rule_verdict(candidate, plan, profile, config, opening)
        elif decision is None:
            # The AI only sees the best finalists; the rest can be watched, not bought.
            verdict, reason, trigger = _rule_verdict(candidate, plan, profile, config, opening)
            if verdict == "COMPRAR":
                verdict, trigger = "VIGILAR", _watch_trigger(candidate, plan)
                reason = (
                    f"no entró entre los {config.screener.max_finalists} finalistas que "
                    "analizó la IA"
                )
        elif decision.plan is None:
            verdict, reason = "NO_HOY", _ai_discard_reason(decision)
        else:
            # The controls run again on the AI's levels, and they still veto.
            plan = decision.plan
            results = tuple(ctl.run_controls(replace(item.context, plan=plan)))
            if ctl.is_blocked(results):
                verdict, reason = "NO_HOY", "; ".join(ctl.blocking_reasons(results))
            else:
                verdict, reason, trigger = _rule_verdict(candidate, plan, profile, config, opening)
                if verdict == "COMPRAR" and decision.verdict == "VIGILAR":
                    verdict, reason = "VIGILAR", "la IA pide esperar una confirmación"
                    trigger = decision.watch_trigger or _watch_trigger(candidate, plan)
                elif verdict == "COMPRAR" and decision.probability < profile.min_probability:
                    verdict, trigger = "VIGILAR", _watch_trigger(candidate, plan)
                    reason = (
                        f"la IA estima {decision.probability:.0%} de probabilidad de tocar el "
                        f"objetivo antes que el stop; el mínimo es {profile.min_probability:.0%}"
                    )
        # Expectancy after costs, with the calibrated probability of the AI.
        outlook: Expectancy | None = None
        if candidate.setup is not None and plan is not None and not ctl.is_blocked(results):
            probability = None
            if decision is not None and decision.plan is not None:
                probability = decision.probability
                if calibrate is not None and probability is not None:
                    probability = min(1.0, max(0.0, calibrate(probability)))
            outlook = expectancy(plan, probability, profile.min_probability, config.costs)
            if verdict == "COMPRAR" and not outlook.passes:
                verdict, trigger = "VIGILAR", _watch_trigger(candidate, plan)
                reason = (
                    f"expectativa de {outlook.ev_r:+.2f}R después de costos; "
                    f"el mínimo es +{MIN_EV_R:.2f}R"
                )
        if verdict == "COMPRAR" and brake is not None and not brake.allows_buying:
            verdict, trigger = "VIGILAR", _watch_trigger(candidate, plan)
            reason = f"freno automático: {brake.text}"
        # An idea that only lacks room or cash is a candidate for a rotation.
        blocked_by = {r.id for r in results if r.effect == "block"}
        if (
            candidate.setup is not None
            and plan is not None
            and blocked_by
            and blocked_by <= ROOM_CONTROLS
            and (brake is None or brake.allows_buying)
            and _rule_verdict(candidate, plan, profile, config, opening)[0] == "COMPRAR"
        ):
            outlook = expectancy(plan, None, profile.min_probability, config.costs)
            if outlook.passes:
                no_room.append((ticker, outlook.ev_r))
        ideas.append(
            Idea(
                ticker=ticker,
                name=item.name,
                sector_etf=item.sector_etf,
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
                no_ai=decision is None,
                metrics=candidate.metrics,
                ai=decision,
                expectancy=outlook,
            )
        )

    # Give out cash from the best expectancy down, within the limits of positions,
    # cash and sector. What does not fit waits: "COMPRAR si liberas efectivo".
    capital = portfolio.capital
    cash = portfolio.cash
    slots = profile.max_positions - len(portfolio.holdings)
    streak, streak_note = streak_multiplier(history)
    sector_values: dict[str | None, float] = {}
    sized: list[Idea] = []
    waiting: list[tuple[str, float]] = list(no_room)
    for idea in sorted(ideas, key=lambda i: (-(i.ev_r if i.ev_r is not None else -9.0), -i.score)):
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
            waiting.append((idea.ticker, idea.ev_r))
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
        size = position_size(
            idea.plan,
            capital=capital,
            cash=cash,
            profile=profile,
            config=config,
            conviction=conviction_multiplier(idea.probability, profile.min_probability),
            controls=idea.size_multiplier,
            streak=streak,
            sector_value=held if idea.sector_etf else 0.0,
        )
        if size.shares <= 0:
            step = 0.01 if config.capital.fractional_shares else 1.0
            risk_per_share = worst_case_risk(idea.plan)
            if size.risk_budget < risk_per_share * step:
                # The account is too small for even one share at this risk per idea.
                why = (
                    f"el riesgo permitido para esta idea (${size.risk_budget:,.2f}) no alcanza "
                    f"para una acción (riesgo por acción ${risk_per_share:,.2f})"
                )
            elif size.limited_by == "efectivo disponible":
                why = "COMPRAR si liberas efectivo"
                waiting.append((idea.ticker, idea.ev_r))
            else:
                why = f"el {size.limited_by} no alcanza para una acción"
            sized.append(replace(idea, verdict="VIGILAR", reason=why, multipliers=size.multipliers))
            continue
        cash -= size.amount
        slots -= 1
        sector_values[idea.sector_etf] = sector_values.get(idea.sector_etf, 0.0) + size.amount
        sized.append(
            replace(
                idea,
                shares=size.shares,
                amount=size.amount,
                risk_usd=size.risk_usd,
                multipliers=size.multipliers,
                limited_by=size.limited_by,
            )
        )
    rotations = propose_rotations(waiting, portfolio.holdings, profile.min_probability)

    order = {"COMPRAR": 0, "VIGILAR": 1, "NO_HOY": 2}
    ranked = sorted(
        sized,
        key=lambda i: (
            order[i.verdict],
            # Purchases by expectancy; the rest by score.
            -(i.ev_r or 0.0) if i.verdict == "COMPRAR" else 0.0,
            -i.score,
            i.ticker,
        ),
    )
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
        review=review,
        rotations=tuple(rotations),
        brake=brake,
        streak_note=streak_note,
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
    review = plan.review
    if review is not None and review.market_reading:
        lines.append(f"Lectura de la IA: {review.market_reading}")
    if review is not None and not review.used_ai and review.note:
        lines.append(f"Veredictos por reglas ({review.label}): {review.note}.")
    if plan.brake is not None and not plan.brake.allows_buying:
        label = "PAUSA" if plan.brake.state == "PAUSA" else "SOLO VIGILAR"
        lines.append(f"{label}: {plan.brake.text}.")
    if plan.streak_note:
        lines.append(f"Tamaño a la mitad por racha: {plan.streak_note}.")
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
        if idea.probability is not None and idea.verdict != "NO_HOY":
            suffix = f" · prob. {idea.probability:.2f}"
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
        if idea.verdict != "NO_HOY" and idea.expectancy is not None:
            lines.append(f"Expectativa: {idea.expectancy.ev_r:+.2f}R después de costos")
        if idea.notes:
            lines.append(f"Por qué: {'; '.join(idea.notes)}")
        decision = idea.ai
        if decision is not None and decision.plan is not None and idea.verdict != "NO_HOY":
            lines.append(
                f"IA: {decision.probability:.0%} de probabilidad de tocar el objetivo antes que "
                f"el stop · confianza {decision.confidence:.0%} · horizonte "
                f"{decision.horizon_days} días"
            )
            if decision.thesis:
                lines.append(f"Tesis: {decision.thesis}")
            if decision.risks:
                lines.append(f"Riesgos: {'; '.join(decision.risks)}")
            if decision.invalidation:
                lines.append(f"Se invalida si: {decision.invalidation}")
            if decision.corrections:
                lines.append(f"Corregido por el código: {'; '.join(decision.corrections)}")
        if idea.reason:
            lines.append(f"{'Motivo' if idea.verdict == 'NO_HOY' else 'Pendiente'}: {idea.reason}")
        if idea.watch_trigger:
            lines.append(f"Se activa si: {idea.watch_trigger}")
        warnings = [f"{c.id} {c.reason}" for c in idea.controls if c.effect == "reduce"]
        if warnings:
            lines.append(f"Avisos: {'; '.join(warnings)}")
    for rotation in plan.rotations:
        lines += ["", f"Rotación propuesta: {rotation.text}"]
    if buys:
        lines += [
            "",
            "Cómo entrar: orden límite dentro de la zona, nunca a mercado; no entres en los "
            f"primeros {config.entry.skip_first_minutes} minutos de la sesión; si tu casa de "
            "bolsa lo permite, deja también la orden stop en el precio indicado.",
        ]
    return "\n".join(lines)
