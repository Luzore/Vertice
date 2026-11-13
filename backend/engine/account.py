"""The real account, read from the database (principle 7).

A real position exists only because the user confirmed a purchase. From those
confirmations this module works out the cash, the open positions, the recent
results and whether an automatic brake is on.

Cash: ``capital.cash_usd`` in the configuration is the money the user put into
the account (they update it when they deposit or withdraw). Available cash is
that amount minus what the confirmed purchases cost, plus what the confirmed
sales brought in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import clock
from core.settings import AppConfig
from db import models
from engine import risk
from engine.data_engine import last_completed_session
from engine.portfolio import Holding, Portfolio
from engine.risk import AccountHistory, Brake

BRAKE_PREFIX = "brake/"
BRAKE_ON = "ACTIVO"
BRAKE_LIFTED = "levantado"
# Capital when the user lifted the pause: the fall is measured from there afterwards.
DRAWDOWN_REFERENCE = "brake_reference/drawdown"
# Brakes that stay on until the user lifts them.
LATCHING = (risk.BRAKE_WEEK, risk.BRAKE_DRAWDOWN)


def is_voided(position: models.Position) -> bool:
    """A position cancelled by a correction: it never counted."""
    return bool((position.tracking or {}).get("voided"))


def real_positions(session: Session, *, open_only: bool = False) -> list[models.Position]:
    query = select(models.Position).where(models.Position.is_shadow.is_(False))
    if open_only:
        query = query.where(models.Position.state != "CLOSED")
    return [p for p in session.scalars(query.order_by(models.Position.id)) if not is_voided(p)]


def fills_of(session: Session, position_ids: Iterable[int]) -> dict[int, list[models.Fill]]:
    by_position: dict[int, list[models.Fill]] = {}
    ids = list(position_ids)
    if not ids:
        return by_position
    for fill in session.scalars(
        select(models.Fill).where(models.Fill.position_id.in_(ids)).order_by(models.Fill.id)
    ):
        by_position.setdefault(fill.position_id, []).append(fill)
    return by_position


def net_result(fills: Iterable[models.Fill]) -> float:
    """Dollars in minus dollars out, commissions included."""
    total = 0.0
    for fill in fills:
        value = fill.price * fill.qty
        total += value - fill.commission if fill.side == "sell" else -(value + fill.commission)
    return total


def cash_balance(session: Session, config: AppConfig) -> float:
    positions = real_positions(session)
    fills = fills_of(session, (p.id for p in positions))
    return config.capital.cash_usd + sum(net_result(fills.get(p.id, [])) for p in positions)


def last_prices(session: Session, tickers: Iterable[str], as_of: datetime) -> dict[str, float]:
    """Latest close of each ticker among the sessions already complete at ``as_of``."""
    wanted = sorted(set(tickers))
    if not wanted:
        return {}
    session_day = last_completed_session(as_of)
    latest = (
        select(models.BarDaily.ticker, func.max(models.BarDaily.session_date).label("day"))
        .where(models.BarDaily.ticker.in_(wanted), models.BarDaily.session_date <= session_day)
        .group_by(models.BarDaily.ticker)
        .subquery()
    )
    rows = session.execute(
        select(models.BarDaily.ticker, models.BarDaily.close).join(
            latest,
            (models.BarDaily.ticker == latest.c.ticker)
            & (models.BarDaily.session_date == latest.c.day),
        )
    )
    return {ticker: float(close) for ticker, close in rows}


def equity_before(session: Session, day: date) -> float | None:
    """Capital recorded at the last close before ``day``."""
    return session.scalar(
        select(models.MetricsDaily.equity_real)
        .where(models.MetricsDaily.day < day, models.MetricsDaily.equity_real.is_not(None))
        .order_by(models.MetricsDaily.day.desc())
        .limit(1)
    )


def load_portfolio(
    session: Session,
    config: AppConfig,
    *,
    as_of: datetime | None = None,
    prices: Mapping[str, float] | None = None,
) -> Portfolio:
    """Cash, open real positions and the result of the day and of the week.

    ``prices`` are live prices when there are any; otherwise the last close is used.
    """
    as_of = as_of or clock.now()
    today = clock.market_date(as_of)
    positions = real_positions(session, open_only=True)
    known = last_prices(session, (p.ticker for p in positions), as_of) | dict(prices or {})
    ideas = {
        idea.id: idea
        for idea in session.scalars(
            select(models.Idea).where(models.Idea.id.in_([p.idea_id for p in positions]))
        )
    }
    holdings = []
    for position in positions:
        idea, tracking = ideas.get(position.idea_id), position.tracking or {}
        targets = list(position.targets or [])
        target = None
        if targets:
            target = (
                targets[1] if tracking.get("target_1_done") and len(targets) > 1 else targets[0]
            )
        holdings.append(
            Holding(
                ticker=position.ticker,
                sector_etf=tracking.get("sector_etf"),
                shares=position.qty_open,
                entry_price=position.avg_price,
                last_price=known.get(position.ticker, position.avg_price),
                stop=position.stop,
                opened_on=clock.market_date(position.opened_at),
                initial_stop=position.avg_price - position.initial_r,
                target=target,
                probability=idea.prob_calibrated if idea is not None else None,
            )
        )
    portfolio = Portfolio(cash=cash_balance(session, config), holdings=tuple(holdings))
    capital = portfolio.capital

    def change_since(reference: float | None) -> float:
        return (capital / reference - 1) * 100 if reference else 0.0

    monday = today - timedelta(days=today.weekday())
    stopped = {}
    for confirmation in session.scalars(
        select(models.Confirmation).where(models.Confirmation.kind == "sold")
    ):
        data = confirmation.data or {}
        if data.get("reason") == "stop" and data.get("ticker"):
            day = date.fromisoformat(data["day"])
            stopped[data["ticker"]] = max(day, stopped.get(data["ticker"], day))
    return Portfolio(
        cash=portfolio.cash,
        holdings=portfolio.holdings,
        stopped_out=stopped,
        day_pnl_pct=change_since(equity_before(session, today)),
        week_pnl_pct=change_since(equity_before(session, monday)),
    )


def load_history(session: Session) -> AccountHistory:
    """Closed real trades and the capital at each close, oldest first."""
    closed = [p for p in real_positions(session) if p.state == "CLOSED"]
    closed.sort(key=lambda p: p.closed_at)
    fills = fills_of(session, (p.id for p in closed))
    curve = session.execute(
        select(models.MetricsDaily.day, models.MetricsDaily.equity_real)
        .where(models.MetricsDaily.equity_real.is_not(None))
        .order_by(models.MetricsDaily.day)
    )
    return AccountHistory(
        closed_results=tuple(net_result(fills.get(p.id, [])) for p in closed),
        equity_curve=tuple((day, float(value)) for day, value in curve),
    )


# --- brakes ------------------------------------------------------------------------------------


def latched_brakes(session: Session) -> tuple[str, ...]:
    rows = session.scalars(
        select(models.SystemStatus).where(
            models.SystemStatus.component.startswith(BRAKE_PREFIX),
            models.SystemStatus.status == BRAKE_ON,
        )
    )
    return tuple(row.component.removeprefix(BRAKE_PREFIX) for row in rows)


def _week_start(moment: datetime) -> date:
    day = clock.market_date(moment)
    return day - timedelta(days=day.weekday())


def _drawdown(session: Session, history: AccountHistory, capital: float) -> float:
    """Fall from the peak, or from the capital at which the user lifted the last pause."""
    reference = session.get(models.SystemStatus, DRAWDOWN_REFERENCE)
    if reference is None:
        return risk.drawdown_pct(history, capital)
    since = clock.market_date(reference.updated_at)
    values = [float(reference.message)]
    values += [value for day, value in history.equity_curve if day > since]
    peak = max([*values, capital])
    return (peak - capital) / peak * 100 if peak > 0 else 0.0


def current_brake(
    session: Session, config: AppConfig, portfolio: Portfolio, history: AccountHistory
) -> Brake:
    """The brake in force. The weekly brake and the pause are remembered in the
    database once they trip, and stay on until the user lifts them. Once lifted,
    the same loss does not trip them again: the weekly brake stays off for the rest
    of that week and the pause measures the fall from the capital at that moment."""
    week_pnl = portfolio.week_pnl_pct
    week_row = session.get(models.SystemStatus, f"{BRAKE_PREFIX}{risk.BRAKE_WEEK}")
    if (
        week_row is not None
        and week_row.status == BRAKE_LIFTED
        and _week_start(week_row.updated_at) == _week_start(clock.now())
    ):
        week_pnl = 0.0
    brake = risk.evaluate_brakes(
        day_pnl_pct=portfolio.day_pnl_pct,
        week_pnl_pct=week_pnl,
        drawdown=_drawdown(session, history, portfolio.capital),
        controls=config.controls,
        latched=latched_brakes(session),
    )
    for name, reason in zip(brake.active, brake.reasons, strict=True):
        if name not in LATCHING:
            continue
        component = f"{BRAKE_PREFIX}{name}"
        row = session.get(models.SystemStatus, component)
        if row is None:
            row = models.SystemStatus(component=component, status=BRAKE_ON, updated_at=clock.now())
            session.add(row)
        if row.status != BRAKE_ON or row.message is None:
            row.status, row.message, row.updated_at = BRAKE_ON, reason, clock.now()
    return brake


def lift_brake(session: Session, name: str, *, capital: float, note: str = "") -> bool:
    """The user reactivates purchases after a weekly brake or a pause. False when
    that brake was not on. ``capital`` is the capital at this moment."""
    row = session.get(models.SystemStatus, f"{BRAKE_PREFIX}{name}")
    if row is None or row.status != BRAKE_ON:
        return False
    row.status, row.updated_at = BRAKE_LIFTED, clock.now()
    row.message = f"{row.message} — levantado por el usuario" + (f": {note}" if note else "")
    if name == risk.BRAKE_DRAWDOWN:
        reference = session.get(models.SystemStatus, DRAWDOWN_REFERENCE)
        if reference is None:
            reference = models.SystemStatus(
                component=DRAWDOWN_REFERENCE, status="ok", updated_at=clock.now()
            )
            session.add(reference)
        reference.message, reference.updated_at = f"{capital:.2f}", clock.now()
    return True
