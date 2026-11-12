"""Database schema (ROADMAP section 15).

The database is the single source of truth (principle 7). Rules that must hold no
matter which code writes the row are CHECK constraints here, not application code.
The most important one is on ``lessons``: a lesson cannot be ``active`` without the
required evidence (principle 9).

Instants are stored in UTC (``UTCDateTime``); session dates are plain dates in
New York time.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from db.types import JSONText, UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Vocabulary fixed by the roadmap. Lists are spelled out so the CHECK constraints
# and the application share one definition.
SNAPSHOT_STATUSES = ("ok", "slow", "error", "stale")
RUN_KINDS = ("premarket", "intraday", "close", "replay")
REGIMES = ("ALCISTA", "NEUTRAL", "RIESGO_ALTO")
SETUPS = ("RUPTURA", "PULLBACK", "CATALIZADOR", "REBOTE")
VERDICTS = ("COMPRAR", "VIGILAR", "NO_HOY")
IDEA_STATES = ("PROPOSED", "BOUGHT", "DECLINED", "SHADOW", "EXPIRED", "NOT_FILLABLE")
POSITION_STATES = ("OPEN", "SELL_ALERT", "PARTIALLY_CLOSED", "CLOSED")
CONTROL_RESULTS = ("pass", "fail", "warn")
FILL_SIDES = ("buy", "sell")
FILL_ORIGINS = ("user", "virtual")
CONFIRMATION_KINDS = ("bought", "declined", "sold", "stop_raised")
ALERT_URGENCIES = ("ALTA", "MEDIA", "BAJA")
ACTORS = ("system", "user")
IMPACTS = ("high", "medium", "low")
CORPORATE_EVENT_TYPES = ("earnings", "filing_8k", "form_4", "offering", "split")
LESSON_KINDS = ("pattern", "own_error", "reasoning")
LESSON_STATUSES = ("hypothesis", "active", "refuted", "retired")

# Evidence a lesson needs before it may influence a decision. These numbers are
# part of the schema: changing them takes a migration, not a config edit.
LESSON_MIN_CASES = 20
LESSON_MIN_FORWARD_CASES = 10
LESSON_MIN_CONFIDENCE = 0.60


def _one_of(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    listed = ",".join(f"'{value}'" for value in values)
    rule = f"{column} IN ({listed})"
    return f"{column} IS NULL OR {rule}" if nullable else rule


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Universe(Base):
    __tablename__ = "universe"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sector: Mapped[str | None] = mapped_column(Text)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    sic_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # SEC identifier and industry code; the sector ETF is derived from the SIC code.
    cik: Mapped[int | None] = mapped_column(Integer)
    sic: Mapped[int | None] = mapped_column(Integer)
    sector_etf: Mapped[str | None] = mapped_column(Text)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)
    profile_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class RawSnapshot(Base):
    """Index of one raw response from a data source, kept 30 days for replay and audit.

    The response itself is a compressed file under the data directory; keeping it
    out of the database keeps the daily database backups small.
    """

    __tablename__ = "raw_snapshots"
    __table_args__ = (
        CheckConstraint(_one_of("status", SNAPSHOT_STATUSES), name="status_valid"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_non_negative"),
        Index(
            "ix_raw_snapshots_source_dataset_key_as_of", "source", "dataset", "request_key", "as_of"
        ),
        Index("ix_raw_snapshots_fetched_at", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    dataset: Mapped[str] = mapped_column(Text, nullable=False)
    # What was asked for (symbols, dates...), so replay can find the same request.
    request_key: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # Clock time the data was requested for (simulated in replay).
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # Real time the request was made.
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Why the status is error, stale or slow; empty when everything went well.
    message: Mapped[str | None] = mapped_column(Text)
    # Path of the compressed payload, relative to the raw data directory.
    payload_path: Mapped[str | None] = mapped_column(Text)
    payload_sha256: Mapped[str | None] = mapped_column(Text)
    payload_bytes: Mapped[int | None] = mapped_column(Integer)


class BarDaily(Base):
    """Split-adjusted daily bar."""

    __tablename__ = "bars_daily"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    vwap: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class BarIntraday(Base):
    """Split-adjusted intraday bar; ``ts`` is the start of the bar."""

    __tablename__ = "bars_intraday"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    timeframe: Mapped[str] = mapped_column(Text, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    vwap: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class News(Base):
    __tablename__ = "news"
    __table_args__ = (
        CheckConstraint("sentiment IS NULL OR sentiment BETWEEN -1 AND 1", name="sentiment_range"),
        UniqueConstraint("url"),
        Index("ix_news_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tickers: Mapped[list[str]] = mapped_column(JSONText, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[float | None] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(Text)
    severe: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class MacroPoint(Base):
    """One observation of a macro series (FRED)."""

    __tablename__ = "macro_points"

    series_id: Mapped[str] = mapped_column(Text, primary_key=True)
    obs_date: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class EconEvent(Base):
    """Scheduled economic release or central bank decision."""

    __tablename__ = "econ_events"
    __table_args__ = (
        CheckConstraint(_one_of("impact", IMPACTS), name="impact_valid"),
        UniqueConstraint("name", "country", "scheduled_at"),
        Index("ix_econ_events_scheduled_at", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    impact: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class CorporateEvent(Base):
    """Earnings date, SEC filing, insider trade, share offering or split."""

    __tablename__ = "corporate_events"
    __table_args__ = (
        CheckConstraint(_one_of("event_type", CORPORATE_EVENT_TYPES), name="event_type_valid"),
        Index("ix_corporate_events_ticker_event_date", "ticker", "event_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payload: Mapped[Any | None] = mapped_column(JSONText)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class IndicatorValue(Base):
    __tablename__ = "indicator_values"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)


class Run(Base):
    """One pass of the pipeline."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_one_of("kind", RUN_KINDS), name="kind_valid"),
        CheckConstraint(_one_of("regime", REGIMES, nullable=True), name="regime_valid"),
        Index("ix_runs_as_of", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Clock time the run worked with (simulated in replay).
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    regime: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[Any | None] = mapped_column(JSONText)


class Candidate(Base):
    """A ticker scored by the screener in one run."""

    __tablename__ = "candidates"
    __table_args__ = (
        CheckConstraint(_one_of("setup", SETUPS, nullable=True), name="setup_valid"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        UniqueConstraint("run_id", "ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    setup: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    breakdown: Mapped[Any] = mapped_column(JSONText, nullable=False)


class ControlResult(Base):
    """Outcome of one control (C01-C22, or one added by a lesson) for a candidate."""

    __tablename__ = "control_results"
    __table_args__ = (
        CheckConstraint(_one_of("result", CONTROL_RESULTS), name="result_valid"),
        UniqueConstraint("candidate_id", "control"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    control: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class AICall(Base):
    """Every call to the AI analyst, with its raw response, usage and cost."""

    __tablename__ = "ai_calls"
    __table_args__ = (
        CheckConstraint("cost_usd IS NULL OR cost_usd >= 0", name="cost_non_negative"),
        Index("ix_ai_calls_created_at", "created_at"),
        Index("ix_ai_calls_dossier_hash", "dossier_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    task: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dossier_hash: Mapped[str] = mapped_column(Text, nullable=False)
    dossier_path: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[str | None] = mapped_column(Text)
    structured_output: Mapped[Any | None] = mapped_column(JSONText)
    usage: Mapped[Any | None] = mapped_column(JSONText)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Idea(Base):
    """One entry of the daily plan."""

    __tablename__ = "ideas"
    __table_args__ = (
        CheckConstraint(_one_of("verdict", VERDICTS), name="verdict_valid"),
        CheckConstraint(_one_of("setup", SETUPS, nullable=True), name="setup_valid"),
        CheckConstraint(_one_of("state", IDEA_STATES), name="state_valid"),
        CheckConstraint("prob_raw IS NULL OR prob_raw BETWEEN 0 AND 1", name="prob_raw_range"),
        CheckConstraint(
            "prob_calibrated IS NULL OR prob_calibrated BETWEEN 0 AND 1",
            name="prob_calibrated_range",
        ),
        CheckConstraint("shares IS NULL OR shares >= 0", name="shares_non_negative"),
        UniqueConstraint("run_id", "ticker"),
        Index("ix_ideas_plan_date", "plan_date"),
        Index("ix_ideas_ticker", "ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"))
    ai_call_id: Mapped[int | None] = mapped_column(ForeignKey("ai_calls.id"))
    plan_date: Mapped[date] = mapped_column(Date, nullable=False)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    setup: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    # True when the verdict came from rules only because the AI was unavailable.
    no_ai: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    prob_raw: Mapped[float | None] = mapped_column(Float)
    prob_calibrated: Mapped[float | None] = mapped_column(Float)
    entry_type: Mapped[str | None] = mapped_column(Text)
    entry_min: Mapped[float | None] = mapped_column(Float)
    entry_max: Mapped[float | None] = mapped_column(Float)
    no_chase_above: Mapped[float | None] = mapped_column(Float)
    watch_trigger: Mapped[str | None] = mapped_column(Text)
    stop: Mapped[float | None] = mapped_column(Float)
    targets: Mapped[Any | None] = mapped_column(JSONText)
    horizon_days: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[float | None] = mapped_column(Float)
    amount_usd: Mapped[float | None] = mapped_column(Float)
    risk_usd: Mapped[float | None] = mapped_column(Float)
    ev_r: Mapped[float | None] = mapped_column(Float)
    thesis: Mapped[str | None] = mapped_column(Text)
    reasons: Mapped[Any | None] = mapped_column(JSONText)
    risks: Mapped[Any | None] = mapped_column(JSONText)
    invalidation: Mapped[str | None] = mapped_column(Text)
    lessons_applied: Mapped[list[str]] = mapped_column(
        JSONText, nullable=False, server_default=text("'[]'")
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PROPOSED'"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Position(Base):
    """A real position (the user confirmed the purchase) or a shadow one."""

    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint(_one_of("state", POSITION_STATES), name="state_valid"),
        CheckConstraint("qty_open >= 0", name="qty_non_negative"),
        CheckConstraint("avg_price > 0", name="avg_price_positive"),
        CheckConstraint("initial_r > 0", name="initial_r_positive"),
        CheckConstraint(
            "(state = 'CLOSED') = (closed_at IS NOT NULL)", name="closed_has_close_time"
        ),
        CheckConstraint("state <> 'CLOSED' OR qty_open = 0", name="closed_has_no_quantity"),
        Index("ix_positions_ticker", "ticker"),
        Index("ix_positions_state", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_id: Mapped[int] = mapped_column(ForeignKey("ideas.id"), nullable=False)
    is_shadow: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    qty_open: Mapped[float] = mapped_column(Float, nullable=False)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop: Mapped[float] = mapped_column(Float, nullable=False)
    targets: Mapped[Any] = mapped_column(JSONText, nullable=False)
    # Initial risk per share: entry price minus initial stop.
    initial_r: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'OPEN'"))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Fill(Base):
    """A purchase or sale: entered by the user, or virtual for shadow positions."""

    __tablename__ = "fills"
    __table_args__ = (
        CheckConstraint(_one_of("side", FILL_SIDES), name="side_valid"),
        CheckConstraint(_one_of("origin", FILL_ORIGINS), name="origin_valid"),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("commission >= 0", name="commission_non_negative"),
        Index("ix_fills_position_id", "position_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    commission: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    origin: Mapped[str] = mapped_column(Text, nullable=False)


class Alert(Base):
    """A sell alert. At most one unresolved alert per position and rule."""

    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(_one_of("urgency", ALERT_URGENCIES), name="urgency_valid"),
        Index(
            "uq_alerts_open_position_rule",
            "position_id",
            "rule",
            unique=True,
            sqlite_where=text("resolved_at IS NULL"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), nullable=False)
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution: Mapped[str | None] = mapped_column(Text)


class Confirmation(Base):
    """What the user confirmed. Corrections add a row pointing at the one they fix."""

    __tablename__ = "confirmations"
    __table_args__ = (CheckConstraint(_one_of("kind", CONFIRMATION_KINDS), name="kind_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    idea_id: Mapped[int | None] = mapped_column(ForeignKey("ideas.id"))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"))
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"))
    corrects_id: Mapped[int | None] = mapped_column(ForeignKey("confirmations.id"))
    data: Mapped[Any | None] = mapped_column(JSONText)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class StateTransition(Base):
    """Audit trail of every state change, with when and who (principle 7)."""

    __tablename__ = "state_transitions"
    __table_args__ = (
        CheckConstraint(_one_of("actor", ACTORS), name="actor_valid"),
        Index("ix_state_transitions_entity", "entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class Lesson(Base):
    """A falsifiable hypothesis about the system's own results (ROADMAP section 13).

    ``active_requires_evidence`` is the rule the whole learning system rests on:
    the database, not the code that writes lessons, decides when one may influence
    a decision.
    """

    __tablename__ = "lessons"
    __table_args__ = (
        CheckConstraint(_one_of("kind", LESSON_KINDS), name="kind_valid"),
        CheckConstraint(_one_of("status", LESSON_STATUSES), name="status_valid"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint(
            "status <> 'active' OR ("
            f"cases_total >= {LESSON_MIN_CASES} "
            f"AND cases_forward >= {LESSON_MIN_FORWARD_CASES} "
            f"AND confidence >= {LESSON_MIN_CONFIDENCE:.2f})",
            name="active_requires_evidence",
        ),
        # Not in the roadmap's SQL; only tightens it. Forward cases are a subset of
        # all cases, so a count that says otherwise is corrupt evidence.
        CheckConstraint(
            "cases_total >= 0 AND cases_forward >= 0 AND cases_forward <= cases_total",
            name="case_counts_consistent",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[Any] = mapped_column(JSONText, nullable=False)
    test: Mapped[Any] = mapped_column(JSONText, nullable=False)
    effect: Mapped[Any] = mapped_column(JSONText, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    cases_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cases_forward: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    proposed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LessonCase(Base):
    """One idea evaluated against a lesson: did the test hold, and was it after
    the lesson was proposed (forward) or before (look-back)."""

    __tablename__ = "lesson_cases"
    __table_args__ = (UniqueConstraint("lesson_id", "idea_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), nullable=False)
    idea_id: Mapped[int] = mapped_column(ForeignKey("ideas.id"), nullable=False)
    holds: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_forward: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class MetricsDaily(Base):
    __tablename__ = "metrics_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    equity_real: Mapped[float | None] = mapped_column(Float)
    equity_system: Mapped[float | None] = mapped_column(Float)
    spy_close: Mapped[float | None] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float)
    drawdown: Mapped[float | None] = mapped_column(Float)
    brier: Mapped[float | None] = mapped_column(Float)


class SystemStatus(Base):
    """Health of each component; feeds the status banner."""

    __tablename__ = "system_status"

    component: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
