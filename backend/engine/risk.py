"""Node 7: the risk engine (ROADMAP section 10).

The AI decides what; this module decides how much (principle 4). Everything here
is deterministic:

* **Size.** A small share of the capital is risked per idea, measured from the
  entry to the stop, scaled by conviction, by the controls and by the recent
  streak, and capped by the limits of the aggressiveness profile and the cash.
* **Expectancy.** An idea is only a purchase if its expected value after costs
  is at least +0.15R.
* **Brakes.** Losses of the day, of the week or from the peak stop new purchases.
* **Rotation.** When a new idea cannot be bought and is clearly better than the
  weakest open position, the plan proposes swapping them. The user decides.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from core.settings import AppConfig, ControlsConfig, CostsConfig
from engine.exits import trading_cost
from engine.portfolio import Holding
from engine.risk_profile import RiskProfile
from engine.trade_plan import TradePlan

MIN_EV_R = 0.15

CONVICTION_MIN = 0.5
CONVICTION_MAX = 1.25
# Probability above the profile's minimum that earns the largest size.
CONVICTION_FULL_ABOVE_MIN = 0.15

STREAK_MULTIPLIER = 0.5
LOSING_STREAK = 3
DRAWDOWN_REDUCE_PCT = 5.0
RECOVERY_SHARE = 0.5

ROTATION_MIN_EDGE_R = 0.5

_EPSILON = 1e-9


# --- conviction and expectancy -----------------------------------------------------------------


def conviction_multiplier(probability: float | None, min_probability: float) -> float:
    """From 0.5 at the minimum probability of the profile to 1.25 fifteen points
    above it, in a straight line. Without a probability (no AI) the size is the
    smallest."""
    if probability is None:
        return CONVICTION_MIN
    ramp = (probability - min_probability) / CONVICTION_FULL_ABOVE_MIN
    ramp = min(1.0, max(0.0, ramp))
    return round(CONVICTION_MIN + (CONVICTION_MAX - CONVICTION_MIN) * ramp, 4)


def worst_case_risk(plan: TradePlan) -> float:
    """Loss per share if the order fills at the top of the entry zone and the stop is hit."""
    return plan.entry_max - plan.stop


@dataclass(frozen=True)
class Expectancy:
    """Expected value of one idea in R, after costs."""

    probability: float
    # True when there was no AI probability and the profile's minimum was assumed.
    assumed_probability: bool
    # Distance to the first target in R, from the middle of the entry zone.
    reward_r: float
    # Commission both ways, tax on it and slippage, as a share of the risk.
    costs_r: float

    @property
    def ev_r(self) -> float:
        return self.probability * self.reward_r - (1 - self.probability) - self.costs_r

    @property
    def passes(self) -> bool:
        return self.ev_r >= MIN_EV_R - _EPSILON


def expectancy(
    plan: TradePlan, probability: float | None, min_probability: float, costs: CostsConfig
) -> Expectancy:
    """EV in R of buying in the middle of the zone: p x reward - (1 - p) - costs.

    The size uses the worst fill of the zone; the expectancy uses the middle,
    because it is an average and not a limit.
    """
    entry = (plan.entry_min + plan.entry_max) / 2
    risk = entry - plan.stop
    round_trip = 2 * trading_cost(entry, 1.0, costs)
    return Expectancy(
        probability=probability if probability is not None else min_probability,
        assumed_probability=probability is None,
        reward_r=(plan.targets[0] - entry) / risk,
        costs_r=round_trip / risk,
    )


# --- size --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sizing:
    shares: float
    # Dollars at the top of the entry zone, and dollars lost if the stop is hit from there.
    amount: float
    risk_usd: float
    # What the formula allowed to risk before the caps.
    risk_budget: float
    # Each factor applied to the base risk, by name.
    multipliers: dict[str, float] = field(default_factory=dict)
    # What capped the size, in Spanish; None when the risk budget did.
    limited_by: str | None = None


def position_size(
    plan: TradePlan,
    *,
    capital: float,
    cash: float,
    profile: RiskProfile,
    config: AppConfig,
    conviction: float,
    controls: float,
    streak: float,
    sector_value: float = 0.0,
) -> Sizing:
    """Shares for one idea: risk budget over risk per share, within every cap.

    ``sector_value`` is what is already held or assigned in the idea's sector.
    """
    multipliers = {"conviccion": conviction, "controles": controls, "racha": streak}
    budget = capital * profile.risk_pct / 100 * conviction * controls * streak
    risk_per_share = worst_case_risk(plan)
    caps = {
        f"tope de {profile.max_position_pct:g}% del capital por posición": (
            profile.max_position_pct / 100 * capital
        ),
        "efectivo disponible": cash,
        f"tope de {config.controls.max_sector_pct:g}% del capital por sector": (
            config.controls.max_sector_pct / 100 * capital - sector_value
        ),
    }
    cap_name, cap_amount = min(caps.items(), key=lambda item: item[1])
    by_risk = budget / risk_per_share
    by_cap = max(0.0, cap_amount) / plan.entry_max
    step = 0.01 if config.capital.fractional_shares else 1.0
    shares = math.floor(min(by_risk, by_cap) / step + _EPSILON) * step
    shares = round(shares, 2)
    return Sizing(
        shares=shares,
        amount=shares * plan.entry_max,
        risk_usd=shares * risk_per_share,
        risk_budget=budget,
        multipliers=multipliers,
        limited_by=cap_name if by_cap < by_risk else None,
    )


# --- streak ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountHistory:
    """What the account did, for the streak multiplier and the brakes."""

    # Net result in dollars of each closed real trade, oldest first.
    closed_results: tuple[float, ...] = ()
    # Capital at each close, oldest first.
    equity_curve: tuple[tuple[date, float], ...] = ()


def _reduced_by_losses(results: Sequence[float]) -> str | None:
    """Three losses in a row halve the size until half of what they lost is back."""
    streak, hole, balance, reduced = 0, 0.0, 0.0, False
    recent: list[float] = []
    for result in results:
        if reduced:
            balance += result
            if balance >= RECOVERY_SHARE * hole:
                reduced, streak, recent = False, 0, []
            continue
        if result < 0:
            streak += 1
            recent.append(result)
            if streak >= LOSING_STREAK:
                reduced, hole, balance = True, -sum(recent[-LOSING_STREAK:]), 0.0
        else:
            streak, recent = 0, []
    if not reduced:
        return None
    return (
        f"{LOSING_STREAK} pérdidas seguidas (${hole:,.0f}); el tamaño vuelve al recuperar "
        f"${RECOVERY_SHARE * hole:,.0f}"
    )


def _reduced_by_drawdown(equity: Sequence[float]) -> str | None:
    """A fall of more than 5% from the peak halves the size until half of it is back."""
    peak, trough, reduced = -math.inf, math.inf, False
    for value in equity:
        if reduced:
            trough = min(trough, value)
            if value >= trough + RECOVERY_SHARE * (peak - trough):
                reduced = False
                peak = max(peak, value)
            continue
        peak = max(peak, value)
        if peak > 0 and (peak - value) / peak * 100 > DRAWDOWN_REDUCE_PCT:
            reduced, trough = True, value
    if not reduced:
        return None
    recover_at = trough + RECOVERY_SHARE * (peak - trough)
    return (
        f"caída de {100 * (peak - trough) / peak:.1f}% desde el máximo de ${peak:,.0f}; "
        f"el tamaño vuelve al recuperar ${recover_at:,.0f}"
    )


def streak_multiplier(history: AccountHistory | None) -> tuple[float, str]:
    """0.5 while the account is in a bad run, 1 otherwise, and why."""
    if history is None:
        return 1.0, ""
    reasons = [
        reason
        for reason in (
            _reduced_by_losses(history.closed_results),
            _reduced_by_drawdown([value for _, value in history.equity_curve]),
        )
        if reason
    ]
    if reasons:
        return STREAK_MULTIPLIER, "; ".join(reasons)
    return 1.0, ""


def drawdown_pct(history: AccountHistory | None, capital: float | None = None) -> float:
    """Fall from the highest capital on record, in percent (0 when at the peak)."""
    values = [value for _, value in history.equity_curve] if history is not None else []
    if capital is not None:
        values.append(capital)
    if not values:
        return 0.0
    peak = max(values)
    return (peak - values[-1]) / peak * 100 if peak > 0 else 0.0


# --- brakes ------------------------------------------------------------------------------------

BrakeState = Literal["NORMAL", "SOLO_VIGILAR", "PAUSA"]
BRAKE_DAY, BRAKE_WEEK, BRAKE_DRAWDOWN = "day", "week", "drawdown"


@dataclass(frozen=True)
class Brake:
    """Whether new purchases are allowed, and why not."""

    state: BrakeState = "NORMAL"
    reasons: tuple[str, ...] = ()
    # Which brakes are on: "day", "week", "drawdown".
    active: tuple[str, ...] = ()

    @property
    def allows_buying(self) -> bool:
        return self.state == "NORMAL"

    @property
    def needs_user(self) -> bool:
        """The weekly brake and the pause stay on until the user lifts them."""
        return BRAKE_WEEK in self.active or BRAKE_DRAWDOWN in self.active

    @property
    def text(self) -> str:
        return "; ".join(self.reasons)


def evaluate_brakes(
    *,
    day_pnl_pct: float,
    week_pnl_pct: float,
    drawdown: float,
    controls: ControlsConfig,
    latched: Sequence[str] = (),
) -> Brake:
    """The automatic brakes. ``latched`` are the ones that tripped before and that
    only the user can lift (the weekly loss and the pause)."""
    active: list[str] = []
    reasons: list[str] = []
    if day_pnl_pct <= -controls.daily_loss_stop_pct + _EPSILON:
        active.append(BRAKE_DAY)
        reasons.append(
            f"pérdida del día de {-day_pnl_pct:.1f}% (freno en {controls.daily_loss_stop_pct:g}%): "
            "sin compras nuevas hasta mañana"
        )
    if week_pnl_pct <= -controls.weekly_loss_stop_pct + _EPSILON or BRAKE_WEEK in latched:
        active.append(BRAKE_WEEK)
        detail = (
            f"pérdida de la semana de {-week_pnl_pct:.1f}%"
            if week_pnl_pct <= -controls.weekly_loss_stop_pct + _EPSILON
            else "freno semanal activo"
        )
        reasons.append(
            f"{detail} (freno en {controls.weekly_loss_stop_pct:g}%): sin compras hasta que "
            "lo reactives"
        )
    if drawdown >= controls.max_drawdown_pause_pct - _EPSILON or BRAKE_DRAWDOWN in latched:
        active.append(BRAKE_DRAWDOWN)
        detail = (
            f"caída de {drawdown:.1f}% desde el máximo"
            if drawdown >= controls.max_drawdown_pause_pct - _EPSILON
            else "pausa activa"
        )
        reasons.append(
            f"{detail} (pausa en {controls.max_drawdown_pause_pct:g}%): revisión obligatoria "
            "de métricas, lecciones y parámetros"
        )
    if BRAKE_DRAWDOWN in active:
        state: BrakeState = "PAUSA"
    elif active:
        state = "SOLO_VIGILAR"
    else:
        state = "NORMAL"
    return Brake(state=state, reasons=tuple(reasons), active=tuple(active))


# --- rotation ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rotation:
    """A proposal to sell a weak position to buy a better idea. Never an order."""

    sell: str
    buy: str
    # Expectancy of the new idea minus what is left in the position, in R.
    edge_r: float
    text: str


def remaining_ev_r(holding: Holding, default_probability: float) -> float | None:
    """What an open position can still be expected to give, in R of its initial risk:
    p x distance to its next target - (1 - p) x distance to its current stop."""
    initial_stop = holding.initial_stop if holding.initial_stop is not None else holding.stop
    risk = holding.entry_price - initial_stop
    if risk <= 0:
        return None
    probability = holding.probability if holding.probability is not None else default_probability
    upside = max(0.0, (holding.target - holding.last_price) / risk) if holding.target else 0.0
    downside = max(0.0, (holding.last_price - holding.stop) / risk)
    return probability * upside - (1 - probability) * downside


def propose_rotations(
    waiting: Sequence[tuple[str, float]],
    holdings: Sequence[Holding],
    default_probability: float,
) -> list[Rotation]:
    """Pair the best ideas left without cash or room with the weakest positions,
    while the new idea beats the position by more than half an R."""
    ranked = sorted(
        (
            (rest, holding)
            for holding in holdings
            if (rest := remaining_ev_r(holding, default_probability)) is not None
        ),
        key=lambda pair: pair[0],
    )
    rotations: list[Rotation] = []
    for (ticker, ev_r), (rest, holding) in zip(
        sorted(waiting, key=lambda idea: -idea[1]), ranked, strict=False
    ):
        edge = ev_r - rest
        if edge <= ROTATION_MIN_EDGE_R or holding.ticker == ticker:
            break
        rotations.append(
            Rotation(
                sell=holding.ticker,
                buy=ticker,
                edge_r=round(edge, 2),
                text=(
                    f"vende {holding.ticker} para comprar {ticker}: a {holding.ticker} le queda "
                    f"una expectativa de {rest:+.2f}R y {ticker} ofrece {ev_r:+.2f}R. "
                    "La decisión es tuya."
                ),
            )
        )
    return rotations
