"""Normalized records (node 2). Every source is converted to these before use.

All instants are timezone-aware UTC and all tickers use one spelling, so the rest
of the pipeline never deals with source-specific formats.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from core import clock
from engine.universe import normalize_ticker

UTCDatetime = Annotated[datetime, AfterValidator(clock.ensure_utc)]
Ticker = Annotated[str, Field(min_length=1), AfterValidator(normalize_ticker)]
Impact = Literal["high", "medium", "low"]
EventType = Literal["earnings", "filing_8k", "form_4", "offering", "split"]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Bar(_Record):
    ticker: Ticker
    timeframe: str
    # Start of the bar.
    ts: UTCDatetime
    # Trading day the bar belongs to, in New York.
    session_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    vwap: float | None = Field(default=None, gt=0)
    source: str

    @model_validator(mode="after")
    def _consistent_range(self) -> Bar:
        if self.high < self.low or not (self.low <= self.close <= self.high):
            raise ValueError("inconsistent bar: close must lie between low and high")
        return self


class Quote(_Record):
    ticker: Ticker
    ts: UTCDatetime
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    bid_size: float = Field(default=0, ge=0)
    ask_size: float = Field(default=0, ge=0)
    source: str

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else None

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        return (self.ask - self.bid) / mid * 100 if mid and self.ask >= self.bid else None


class NewsItem(_Record):
    tickers: tuple[Ticker, ...] = ()
    published_at: UTCDatetime
    source: str
    headline: str = Field(min_length=1)
    summary: str | None = None
    url: str | None = None
    sentiment: float | None = Field(default=None, ge=-1, le=1)
    category: str | None = None
    severe: bool = False


class MacroPoint(_Record):
    series_id: str
    obs_date: date
    value: float
    source: str


class EconEvent(_Record):
    name: str
    country: str
    scheduled_at: UTCDatetime
    impact: Impact
    source: str


class CorporateEvent(_Record):
    ticker: Ticker
    event_type: EventType
    event_date: date
    # Exact time when the source gives one.
    event_at: UTCDatetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str
    url: str | None = None


class Filing(_Record):
    """One SEC filing. Only some forms become corporate events."""

    ticker: Ticker
    cik: int
    form: str
    filed_on: date
    accepted_at: UTCDatetime | None = None
    accession: str
    items: tuple[str, ...] = ()
    url: str | None = None
