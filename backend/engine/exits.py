"""Exit rules applied to a price bar (ROADMAP section 12).

Pure functions: given a position and what the price did, they say what to sell
and where the stop moves. They are code, so they work without the AI (principle 6).
Replay uses them on daily bars to follow the shadow positions; the live watcher
of Phase 6 will use the same rules on real-time prices.

On a daily bar the order of events inside the day is unknown, so the conservative
reading is used: if the stop and a target were both touched, the stop came first.

Golden rule: the stop only moves up, never down.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.settings import CostsConfig, ExitsConfig

STOP = "saltó el stop"
STOP_GAP = "abrió debajo del stop"
TARGET_1 = "alcanzó el objetivo 1"
TARGET_2 = "alcanzó el objetivo 2"
EARNINGS = "resultados: se vende antes del reporte"
TIME_STOP = "stop de tiempo"
HORIZON = "se cumplió el horizonte"
END_OF_TEST = "fin del periodo de prueba"


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Fill:
    day: date
    side: str  # "buy" or "sell"
    price: float
    shares: float
    cost: float  # commission, tax on the commission and slippage, in dollars
    reason: str


def trading_cost(price: float, shares: float, costs: CostsConfig) -> float:
    """Commission, VAT on the commission and estimated slippage for one fill."""
    value = price * shares
    commission = value * costs.commission_pct / 100
    return commission * (1 + costs.vat_on_commission_pct / 100) + value * costs.slippage_pct / 100


@dataclass
class ShadowPosition:
    """A virtual position that follows the plan of one idea."""

    ticker: str
    setup: str | None
    sector_etf: str | None
    entry_day: date
    entry_price: float
    shares: float
    stop: float
    targets: tuple[float, float]
    atr: float
    horizon_days: int
    regime_at_entry: str = ""
    score: float = 0.0
    shares_open: float = field(init=False)
    initial_stop: float = field(init=False)
    highest: float = field(init=False)
    days_held: int = 0
    target_1_done: bool = False
    fills: list[Fill] = field(default_factory=list)
    exit_reason: str | None = None
    exit_day: date | None = None
    last_price: float = field(init=False)

    def __post_init__(self) -> None:
        self.shares_open = self.shares
        self.initial_stop = self.stop
        self.highest = self.entry_price
        self.last_price = self.entry_price

    @property
    def risk_per_share(self) -> float:
        return self.entry_price - self.initial_stop

    @property
    def is_open(self) -> bool:
        return self.shares_open > 0

    def r_multiple(self, price: float) -> float:
        return (price - self.entry_price) / self.risk_per_share

    @property
    def best_r(self) -> float:
        return self.r_multiple(self.highest)

    @property
    def net_pnl(self) -> float:
        """Realized result in dollars after every cost, entry included."""
        bought = sum(f.price * f.shares + f.cost for f in self.fills if f.side == "buy")
        sold = sum(f.price * f.shares - f.cost for f in self.fills if f.side == "sell")
        return sold + self.shares_open * self.last_price - bought

    @property
    def net_r(self) -> float:
        """Net result in R: dollars won or lost over the dollars risked at entry."""
        return self.net_pnl / (self.shares * self.risk_per_share)

    def raise_stop(self, level: float) -> bool:
        """Move the stop up to ``level``. A lower level is ignored: the stop never loosens."""
        if level > self.stop:
            self.stop = level
            return True
        return False


def reports_before_next_open(events: Iterable[Any], day: date, next_day: date) -> bool:
    """True when the company reports on ``day`` after the close or before the open
    of ``next_day`` (the next session). ``events`` are its corporate events."""
    for event in events:
        if event.event_type != "earnings" or event.payload.get("kind") != "scheduled":
            continue
        hour = event.payload.get("hour") or ""
        if event.event_date == day and hour == "amc":
            return True
        if event.event_date == next_day and hour in ("bmo", ""):
            return True
    return False


NOT_FILLABLE_BELOW_STOP = "no ejecutable: abrió debajo del stop"
NOT_FILLABLE_NEVER_IN_ZONE = "no ejecutable: el precio no volvió a la zona de entrada"


def theoretical_entry(entry_max: float, stop: float, bar: Bar) -> tuple[float | None, str]:
    """Where a limit order at the top of the entry zone would have filled on ``bar``,
    or None and why it would not."""
    if bar.open <= stop:
        return None, NOT_FILLABLE_BELOW_STOP
    if bar.open <= entry_max:
        return bar.open, "entró"  # a limit order at the top of the zone fills at the open
    if bar.low <= entry_max:
        return entry_max, "entró"  # opened above the zone and came back into it
    return None, NOT_FILLABLE_NEVER_IN_ZONE


def open_position(position: ShadowPosition, costs: CostsConfig) -> Fill:
    fill = Fill(
        position.entry_day,
        "buy",
        position.entry_price,
        position.shares,
        trading_cost(position.entry_price, position.shares, costs),
        "entrada",
    )
    position.fills.append(fill)
    return fill


def _sell(
    position: ShadowPosition, bar: Bar, price: float, shares: float, reason: str, costs: CostsConfig
) -> Fill:
    shares = min(shares, position.shares_open)
    fill = Fill(bar.day, "sell", price, shares, trading_cost(price, shares, costs), reason)
    position.fills.append(fill)
    position.shares_open -= shares
    if position.shares_open <= 1e-9:
        position.shares_open = 0.0
        position.exit_reason, position.exit_day = reason, bar.day
    return fill


def apply_bar(
    position: ShadowPosition,
    bar: Bar,
    exits: ExitsConfig,
    costs: CostsConfig,
    *,
    entry_day: bool = False,
    earnings_before_next_open: bool = False,
    regime: str = "",
    fractional_shares: bool = False,
) -> list[Fill]:
    """Apply the exit rules to one bar. Returns the sales it caused.

    On the entry day only the stop is checked: the position did not exist for the
    whole bar, so the day's high cannot be credited to it.
    """
    fills: list[Fill] = []
    if not position.is_open:
        return fills
    position.last_price = bar.close

    # Rule 1: the stop. A gap below it is sold at the open, not at the stop price.
    if bar.open <= position.stop and not entry_day:
        fills.append(_sell(position, bar, bar.open, position.shares_open, STOP_GAP, costs))
        return fills
    if bar.low <= position.stop:
        fills.append(_sell(position, bar, position.stop, position.shares_open, STOP, costs))
        return fills

    if not entry_day:
        position.days_held += 1
        position.highest = max(position.highest, bar.high)
        # Rule 3: first target. Sell part and protect the rest at the entry price.
        if not position.target_1_done and bar.high >= position.targets[0]:
            part = position.shares * exits.target1_sell_pct / 100
            if not fractional_shares:
                # Whole shares only: round the half down, and with a single share sell it.
                part = math.floor(part + 1e-9) or position.shares_open
            fills.append(_sell(position, bar, position.targets[0], part, TARGET_1, costs))
            position.target_1_done = True
            if exits.move_stop_to_breakeven_after_t1:
                position.raise_stop(position.entry_price)
        # Rule 5: second target sells the rest.
        if position.is_open and bar.high >= position.targets[1]:
            fills.append(
                _sell(position, bar, position.targets[1], position.shares_open, TARGET_2, costs)
            )
            return fills
        # Rule 4: trailing stop once the trade has gone far enough in its favour.
        if position.best_r >= exits.trailing_start_r:
            position.raise_stop(position.highest - exits.trailing_atr_mult * position.atr)

    if not position.is_open:
        return fills
    # Rule 6: do not hold through an earnings report.
    if earnings_before_next_open and not exits.hold_through_earnings:
        fills.append(_sell(position, bar, bar.close, position.shares_open, EARNINGS, costs))
        return fills
    # Rule 8: in a high-risk market, tighten the stop of a trade that is not yet +1R.
    if regime == "RIESGO_ALTO" and position.r_multiple(bar.close) < 1:
        position.raise_stop(bar.close - position.atr)
    # Rule 9: time stop for a trade that goes nowhere.
    if position.days_held >= exits.time_stop_days and position.best_r < exits.time_stop_min_r:
        fills.append(_sell(position, bar, bar.close, position.shares_open, TIME_STOP, costs))
        return fills
    # Rule 10: the horizon of the idea.
    if position.days_held >= position.horizon_days:
        fills.append(_sell(position, bar, bar.close, position.shares_open, HORIZON, costs))
    return fills


def close_at(position: ShadowPosition, bar: Bar, reason: str, costs: CostsConfig) -> Fill | None:
    """Close what is left at the bar's close (used when a test period ends)."""
    if not position.is_open:
        return None
    position.last_price = bar.close
    return _sell(position, bar, bar.close, position.shares_open, reason, costs)
