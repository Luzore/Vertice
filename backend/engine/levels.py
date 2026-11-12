"""The price levels the AI is allowed to use (principle 3).

The AI chooses *which* level; it never makes one up. This module lists, for one
candidate, every price that may be used as an entry, a stop or a target. The
dossier shows these lists to the AI and the validation compares the AI's answer
against them.

* **Stops** sit just under a technical level (a swing low or a moving average), at
  the distance the controls accept. A bare ATR multiple is not a valid stop:
  control C14 asks for a technical level.
* **Entries** stay within one ATR of the last close.
* **Targets** are resistances above the price, or multiples of the ATR or of R.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from core.settings import ControlsConfig
from engine.indicators import Levels
from engine.trade_plan import STOP_BUFFER_ATR, TradePlan, technical_levels_below

ENTRY_ATR_STEPS = (0.25, 0.5, 0.75, 1.0)
ENTRY_MAX_DISTANCE_ATR = 1.0
TARGET_ATR_MULTIPLES = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0)
TARGET_R_MULTIPLES = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0)
_EPSILON = 1e-9


@dataclass(frozen=True)
class NamedLevel:
    price: float
    # What the level is, in Spanish (shown to the AI and in corrections).
    name: str
    # For a stop: the technical level it sits under, and what that level is.
    anchor: float | None = None
    anchor_name: str | None = None


@dataclass(frozen=True)
class ValidLevels:
    # The last close, which every distance is measured from, and the 14-day ATR.
    reference: float
    atr: float
    entries: tuple[NamedLevel, ...]
    stops: tuple[NamedLevel, ...]
    # Targets that do not depend on the stop; ``targets_for`` adds the multiples of R.
    targets: tuple[NamedLevel, ...]

    def targets_for(self, stop: float) -> tuple[NamedLevel, ...]:
        """Every valid target once the stop is known: R is close minus stop."""
        risk = self.reference - stop
        if risk <= 0:
            return self.targets
        multiples = [
            NamedLevel(round(self.reference + k * risk, 2), f"{k:g}R sobre el cierre")
            for k in TARGET_R_MULTIPLES
        ]
        return _unique([*self.targets, *multiples])


def _unique(levels: Iterable[NamedLevel]) -> tuple[NamedLevel, ...]:
    """Sorted by price, one entry per price (the first name wins)."""
    seen: dict[float, NamedLevel] = {}
    for level in levels:
        if level.price > 0 and not math.isnan(level.price):
            seen.setdefault(level.price, level)
    return tuple(sorted(seen.values(), key=lambda level: level.price))


def _metric(metrics: Mapping[str, float], name: str) -> float:
    value = metrics.get(name)
    return float("nan") if value is None else float(value)


def valid_levels(
    metrics: Mapping[str, float],
    levels: Levels,
    controls: ControlsConfig,
    plan: TradePlan | None = None,
) -> ValidLevels | None:
    """The valid levels of one ticker, or None without a price and an ATR.

    The levels of the rule-based ``plan`` are always valid: using that plan
    unchanged can never be a mistake.
    """
    close, atr = _metric(metrics, "close"), _metric(metrics, "atr_14")
    if math.isnan(close) or math.isnan(atr) or atr <= 0 or close <= 0:
        return None

    # Stops: just under each technical level, at a distance the controls accept.
    stops = [
        NamedLevel(
            round(level - STOP_BUFFER_ATR * atr, 2),
            f"bajo el {name} de {level:,.2f}",
            anchor=round(level, 4),
            anchor_name=name,
        )
        for level, name in technical_levels_below(metrics, levels)
    ]
    if plan is not None and plan.stop_level is not None:
        stops.append(
            NamedLevel(
                plan.stop,
                "stop del plan de reglas",
                anchor=plan.stop_level,
                anchor_name=plan.stop_level_name,
            )
        )
    stops = [
        stop
        for stop in stops
        if controls.stop_min_atr - _EPSILON
        <= (close - stop.price) / atr
        <= controls.stop_max_atr + _EPSILON
    ]

    # Entries: the close, steps of a quarter ATR around it and technical levels nearby.
    entries = [NamedLevel(round(close, 2), "último cierre")]
    for step in ENTRY_ATR_STEPS:
        entries.append(NamedLevel(round(close - step * atr, 2), f"{step:g} ATR bajo el cierre"))
        entries.append(NamedLevel(round(close + step * atr, 2), f"{step:g} ATR sobre el cierre"))
    nearby = [
        (_metric(metrics, "sma_20"), "promedio de 20 días"),
        (_metric(metrics, "ema_21"), "promedio exponencial de 21 días"),
        (_metric(metrics, "prior_close_max_20d"), "máximo de cierre de 20 días"),
        (_metric(metrics, "donchian_high_20"), "máximo de 20 días"),
        *((level, "soporte") for level in levels.supports),
        *((level, "resistencia") for level in levels.resistances),
    ]
    entries += [
        NamedLevel(round(price, 2), name)
        for price, name in nearby
        if not math.isnan(price) and abs(price - close) <= ENTRY_MAX_DISTANCE_ATR * atr
    ]
    if plan is not None:
        entries += [
            NamedLevel(plan.entry_min, "zona del plan de reglas"),
            NamedLevel(plan.entry_max, "zona del plan de reglas"),
            NamedLevel(plan.no_chase_above, "no perseguir del plan de reglas"),
        ]

    # Targets: resistances above the price and ATR multiples.
    targets = [
        NamedLevel(round(level, 2), "resistencia") for level in levels.resistances if level > close
    ]
    high_52w = _metric(metrics, "high_52w")
    if not math.isnan(high_52w) and high_52w > close:
        targets.append(NamedLevel(round(high_52w, 2), "máximo de 52 semanas"))
    targets += [
        NamedLevel(round(close + k * atr, 2), f"{k:g} ATR sobre el cierre")
        for k in TARGET_ATR_MULTIPLES
    ]
    if plan is not None:
        targets += [NamedLevel(target, "objetivo del plan de reglas") for target in plan.targets]

    return ValidLevels(
        reference=close,
        atr=atr,
        entries=_unique(entries),
        stops=_unique(stops),
        targets=_unique(targets),
    )
