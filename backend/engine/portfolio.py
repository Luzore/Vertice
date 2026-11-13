"""What the controls need to know about the account at one moment."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Holding:
    ticker: str
    sector_etf: str | None
    shares: float
    entry_price: float
    last_price: float
    stop: float
    opened_on: date
    # The stop the position was opened with; ``stop`` may have been raised since.
    initial_stop: float | None = None
    # Its next target and the probability its idea was given, for rotations.
    target: float | None = None
    probability: float | None = None

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price

    @property
    def r_now(self) -> float | None:
        """Open result in R: how many times the initial risk the price has moved."""
        risk = self.entry_price - (
            self.initial_stop if self.initial_stop is not None else self.stop
        )
        return (self.last_price - self.entry_price) / risk if risk > 0 else None


@dataclass(frozen=True)
class Portfolio:
    cash: float
    holdings: tuple[Holding, ...] = ()
    # Ticker -> day it was last sold because the stop was hit (for the cooldown).
    stopped_out: dict[str, date] = field(default_factory=dict)
    # Profit or loss as a percentage of capital; negative is a loss.
    day_pnl_pct: float = 0.0
    week_pnl_pct: float = 0.0

    @property
    def capital(self) -> float:
        """Cash plus the market value of the open positions."""
        return self.cash + sum(h.market_value for h in self.holdings)

    def sector_value(self, sector_etf: str | None) -> float:
        return sum(h.market_value for h in self.holdings if h.sector_etf == sector_etf)

    def sector_count(self, sector_etf: str | None) -> int:
        return sum(1 for h in self.holdings if h.sector_etf == sector_etf)

    def holds(self, ticker: str) -> bool:
        return any(h.ticker == ticker for h in self.holdings)
