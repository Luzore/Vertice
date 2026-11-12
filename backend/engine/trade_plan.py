"""Entry zone, stop and targets from the valid levels of a ticker (principle 3).

Every price here comes from a level the data supports: a swing low or a moving
average for the stop, a swing high for the targets, or a multiple of the ATR when
there is no level. This is the plan the rules produce without the AI, and the
set of levels the AI's numbers are checked against in Phase 4.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from engine.indicators import Levels
from engine.risk_profile import RiskProfile

ENTRY_HALF_WIDTH_ATR = 0.25
NO_CHASE_ATR = 0.75
STOP_BUFFER_ATR = 0.10
STOP_MIN_ATR = 0.8
STOP_MAX_ATR = 3.0


@dataclass(frozen=True)
class TradePlan:
    # Limit order zone; never buy above ``no_chase_above``.
    entry_min: float
    entry_max: float
    no_chase_above: float
    # The price the plan is built around: the last close.
    reference: float
    stop: float
    # The technical level the stop sits under, or None when it is only an ATR multiple.
    stop_level: float | None
    stop_level_name: str | None
    targets: tuple[float, float]
    atr: float

    @property
    def risk_per_share(self) -> float:
        """R: what is lost per share if the stop is hit."""
        return self.reference - self.stop

    @property
    def stop_atr(self) -> float:
        return self.risk_per_share / self.atr

    def r_multiple(self, price: float) -> float:
        return (price - self.reference) / self.risk_per_share

    @property
    def reward_r(self) -> float:
        """Distance to the first target in R."""
        return self.r_multiple(self.targets[0])


def technical_levels_below(metrics: Mapping[str, float], levels: Levels) -> list[tuple[float, str]]:
    """Levels under the last close where a stop can sit, each with its name."""
    close = metrics["close"]
    named = [(level, "soporte") for level in levels.supports]
    for metric, name in (
        ("sma_20", "promedio de 20 días"),
        ("ema_21", "promedio exponencial de 21 días"),
        ("sma_50", "promedio de 50 días"),
        ("donchian_low_20", "mínimo de 20 días"),
    ):
        value = metrics.get(metric)
        if value is not None and not math.isnan(value):
            named.append((float(value), name))
    return sorted(((level, name) for level, name in named if level < close), reverse=True)


def build_trade_plan(
    metrics: Mapping[str, float], levels: Levels, profile: RiskProfile
) -> TradePlan | None:
    """The rule-based plan for one ticker, or None without a price and an ATR."""
    close, atr = metrics.get("close"), metrics.get("atr_14")
    if close is None or atr is None or math.isnan(close) or math.isnan(atr) or atr <= 0:
        return None

    # Stop: under the technical level whose distance is closest to the profile's.
    best: tuple[float, float, str] | None = None
    for level, name in technical_levels_below(metrics, levels):
        stop = level - STOP_BUFFER_ATR * atr
        distance = (close - stop) / atr
        if STOP_MIN_ATR <= distance <= STOP_MAX_ATR and (
            best is None
            or abs(distance - profile.stop_atr) < abs((close - best[0]) / atr - profile.stop_atr)
        ):
            best = (stop, level, name)
    if best is not None:
        stop, stop_level, stop_name = best
    else:
        stop, stop_level, stop_name = close - profile.stop_atr * atr, None, None
    stop = round(stop, 2)
    risk = close - stop
    if risk <= 0:
        return None

    # Targets: the swing highs above; multiples of R when there are none.
    above = [level for level in levels.resistances if level > close]
    target_1 = above[0] if above else close + 2 * risk
    further = [level for level in above[1:] if level >= target_1 + 0.5 * risk]
    target_2 = further[0] if further else max(close + 3 * risk, target_1 + risk)
    return TradePlan(
        entry_min=round(close - ENTRY_HALF_WIDTH_ATR * atr, 2),
        entry_max=round(close + ENTRY_HALF_WIDTH_ATR * atr, 2),
        no_chase_above=round(close + NO_CHASE_ATR * atr, 2),
        reference=round(close, 4),
        stop=stop,
        stop_level=round(stop_level, 4) if stop_level is not None else None,
        stop_level_name=stop_name,
        targets=(round(target_1, 2), round(target_2, 2)),
        atr=atr,
    )
