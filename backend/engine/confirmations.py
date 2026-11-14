"""Node 8: manual confirmation (ROADMAP section 11).

The system never buys or sells (principle 1). The user does, and tells the system
what happened:

* **"La compré"** creates the real position with the price actually paid, and
  recalculates R from it.
* **"No la compré"** leaves the idea to be followed as a shadow.
* **"Vendí"** records a partial or total sale, and answers the alert that asked for it.
* **"Ya subí el stop"** moves the stop up (never down); **"Ignorar"** dismisses an
  alert with a reason.
* At the close, ideas nobody answered become shadows too, when the price did
  reach the entry zone that day; otherwise they are "not fillable".

Every change of state goes through the state machine and every confirmation is
kept: a correction adds a row that points at the one it fixes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import clock, market_calendar
from core.settings import AppConfig
from db import models
from engine import exits, states, watcher
from engine.account import fills_of, is_voided, net_result
from engine.plan_store import todays_ideas

DEFAULT_HORIZON_DAYS = 10
DECLINE_REASONS = ("sin efectivo", "no me convenció", "llegué tarde", "otro")
SELL_REASONS = ("stop", "objetivo", "tiempo", "resultados", "decisión propia", "otro")


class ConfirmationError(ValueError):
    """What the user sent cannot be applied as it is; nothing was changed."""


class NotFound(LookupError):
    """There is no such idea, position or confirmation."""


def _idea(session: Session, idea_id: int) -> models.Idea:
    idea = session.get(models.Idea, idea_id)
    if idea is None:
        raise NotFound(f"no existe la idea {idea_id}")
    return idea


def _position_of(
    session: Session, idea_id: int, *, shadow: bool, open_only: bool = False
) -> models.Position | None:
    query = select(models.Position).where(
        models.Position.idea_id == idea_id, models.Position.is_shadow.is_(shadow)
    )
    candidates = [
        p for p in session.scalars(query.order_by(models.Position.id)) if not is_voided(p)
    ]
    if open_only:
        candidates = [p for p in candidates if p.state != "CLOSED"]
    return candidates[-1] if candidates else None


def _last_confirmation(session: Session, idea_id: int) -> models.Confirmation | None:
    return session.scalars(
        select(models.Confirmation)
        .where(models.Confirmation.idea_id == idea_id)
        .order_by(models.Confirmation.id.desc())
        .limit(1)
    ).first()


def _tracking(idea: models.Idea, price: float, shares: float) -> dict[str, Any]:
    """What the exit rules need to remember about a position."""
    details = idea.details or {}
    run = idea.run_id
    return {
        "atr": details.get("atr"),
        "highest": price,
        "days_held": 0,
        "target_1_done": False,
        "horizon_days": idea.horizon_days or DEFAULT_HORIZON_DAYS,
        "initial_shares": shares,
        "setup": idea.setup,
        "sector_etf": details.get("sector_etf"),
        "score": idea.score,
        "last_price": price,
        "run_id": run,
    }


def _void(session: Session, position: models.Position, reason: str) -> None:
    """Cancel a position that should never have counted (a correction)."""
    if position.state != "CLOSED":
        states.transition(session, "position", position, "CLOSED", actor="user", reason=reason)
        position.qty_open, position.closed_at = 0.0, clock.now()
    position.tracking = {**(position.tracking or {}), "voided": True, "voided_reason": reason}


def _recompute(session: Session, position: models.Position, initial_stop: float) -> None:
    """Average price, open quantity and initial R of a position from its fills."""
    session.flush()
    fills = fills_of(session, [position.id]).get(position.id, [])
    bought = sum(f.qty for f in fills if f.side == "buy")
    sold = sum(f.qty for f in fills if f.side == "sell")
    if bought <= 0:
        raise ConfirmationError("la posición quedaría sin ninguna compra")
    if sold > bought + 1e-9:
        raise ConfirmationError(
            f"ya vendiste {sold:g} acciones: la compra no puede quedar en {bought:g}"
        )
    average = sum(f.price * f.qty for f in fills if f.side == "buy") / bought
    if average <= initial_stop:
        raise ConfirmationError(
            f"el precio promedio (${average:,.2f}) debe ser mayor al stop (${initial_stop:,.2f})"
        )
    position.avg_price = average
    position.qty_open = round(bought - sold, 6)
    position.initial_r = average - initial_stop


# --- "La compré" -------------------------------------------------------------------------------


def confirm_bought(
    session: Session,
    idea_id: int,
    *,
    price: float,
    qty: float,
    filled_at: datetime | None = None,
    commission: float = 0.0,
    note: str | None = None,
) -> models.Position:
    """The user bought. Creates the real position (or adds to it) at the price paid."""
    idea = _idea(session, idea_id)
    if not price > 0 or not qty > 0 or commission < 0:
        raise ConfirmationError("el precio y la cantidad deben ser mayores a cero")
    if idea.stop is None or not idea.targets:
        raise ConfirmationError(
            f"la idea de {idea.ticker} no tiene stop ni objetivos: no se puede seguir como posición"
        )
    if price <= idea.stop:
        raise ConfirmationError(
            f"el precio de compra (${price:,.2f}) debe ser mayor al stop (${idea.stop:,.2f})"
        )
    filled_at = clock.ensure_utc(filled_at) if filled_at is not None else clock.now()
    if filled_at > clock.now():
        raise ConfirmationError("la hora de la compra no puede estar en el futuro")
    previous = _last_confirmation(session, idea_id)
    position = _position_of(session, idea_id, shadow=False, open_only=True)
    correction = idea.state not in ("PROPOSED", "BOUGHT")

    if idea.state != "BOUGHT":
        states.transition(
            session, "idea", idea, "BOUGHT", actor="user", reason="La compré", correction=correction
        )
        shadow = _position_of(session, idea_id, shadow=True)
        if shadow is not None:
            _void(session, shadow, "confirmaste la compra: la sombra se anula")
    if position is None:
        position = models.Position(
            idea_id=idea.id,
            is_shadow=False,
            ticker=idea.ticker,
            qty_open=qty,
            avg_price=price,
            stop=idea.stop,
            targets=list(idea.targets),
            initial_r=price - idea.stop,
            state="OPEN",
            opened_at=filled_at,
            tracking=_tracking(idea, price, qty),
        )
        session.add(position)
        session.flush()
        states.record_creation(
            session, "position", position, actor="user", reason="compra confirmada"
        )
    fill = models.Fill(
        position_id=position.id,
        side="buy",
        price=price,
        qty=qty,
        filled_at=filled_at,
        commission=commission,
        origin="user",
    )
    session.add(fill)
    _recompute(session, position, idea.stop)
    tracking = dict(position.tracking or {})
    tracking["initial_shares"] = sum(
        f.qty for f in fills_of(session, [position.id])[position.id] if f.side == "buy"
    )
    position.tracking = tracking
    session.add(
        models.Confirmation(
            kind="bought",
            idea_id=idea.id,
            position_id=position.id,
            corrects_id=previous.id if correction and previous is not None else None,
            data={
                "fill_id": fill.id,
                "ticker": idea.ticker,
                "price": price,
                "qty": qty,
                "commission": commission,
                "filled_at": filled_at.isoformat(),
            },
            note=note,
            created_at=clock.now(),
        )
    )
    return position


def correct_purchase(
    session: Session,
    confirmation_id: int,
    *,
    price: float | None = None,
    qty: float | None = None,
    filled_at: datetime | None = None,
    commission: float | None = None,
    note: str | None = None,
) -> models.Position:
    """Fix the data of a purchase already confirmed. The original confirmation is
    kept and a new one, pointing at it, records the change."""
    original = session.get(models.Confirmation, confirmation_id)
    if original is None or original.kind != "bought" or not (original.data or {}).get("fill_id"):
        raise NotFound(f"no existe la confirmación de compra {confirmation_id}")
    fill = session.get(models.Fill, original.data["fill_id"])
    position = session.get(models.Position, original.position_id)
    idea = _idea(session, original.idea_id)
    if position is None or fill is None or is_voided(position):
        raise ConfirmationError("esa compra fue anulada y ya no se puede corregir")
    before = {"price": fill.price, "qty": fill.qty, "commission": fill.commission}
    if price is not None:
        fill.price = price
    if qty is not None:
        fill.qty = qty
    if commission is not None:
        fill.commission = commission
    if filled_at is not None:
        fill.filled_at = clock.ensure_utc(filled_at)
    if not fill.price > 0 or not fill.qty > 0 or fill.commission < 0:
        raise ConfirmationError("el precio y la cantidad deben ser mayores a cero")
    _recompute(session, position, idea.stop)
    if position.qty_open <= 0 and position.state != "CLOSED":
        raise ConfirmationError("con esa cantidad la posición quedaría en cero: registra la venta")
    session.add(
        models.Confirmation(
            kind="bought",
            idea_id=idea.id,
            position_id=position.id,
            corrects_id=original.id,
            data={
                "fill_id": fill.id,
                "ticker": idea.ticker,
                "price": fill.price,
                "qty": fill.qty,
                "commission": fill.commission,
                "filled_at": fill.filled_at.isoformat(),
                "before": before,
            },
            note=note,
            created_at=clock.now(),
        )
    )
    return position


# --- "No la compré" ----------------------------------------------------------------------------


def confirm_declined(
    session: Session, idea_id: int, *, reason: str | None = None, note: str | None = None
) -> models.Idea:
    """The user did not buy. Undoing a purchase entered by mistake is a correction,
    allowed only while nothing was sold."""
    idea = _idea(session, idea_id)
    if idea.state == "DECLINED":
        return idea  # answered already: a repeated click changes nothing
    previous = _last_confirmation(session, idea_id)
    correction = idea.state == "BOUGHT"
    if correction:
        position = _position_of(session, idea_id, shadow=False)
        fills = fills_of(session, [position.id]).get(position.id, []) if position else []
        if any(fill.side == "sell" for fill in fills):
            raise ConfirmationError(
                "esa posición ya tiene ventas registradas: no se puede anular la compra"
            )
        if position is not None:
            _void(session, position, "corrección: no la compré")
    states.transition(
        session,
        "idea",
        idea,
        "DECLINED",
        actor="user",
        reason=f"No la compré: {reason}" if reason else "No la compré",
        correction=correction,
    )
    session.add(
        models.Confirmation(
            kind="declined",
            idea_id=idea.id,
            corrects_id=previous.id if correction and previous is not None else None,
            data={"ticker": idea.ticker, "reason": reason},
            note=note,
            created_at=clock.now(),
        )
    )
    return idea


# --- "Vendí" -----------------------------------------------------------------------------------


# Why a sale happened, from the rule of the alert it answers (the cooldown reads "stop").
_SALE_REASONS = {
    watcher.STOP: "stop",
    watcher.TARGET_1: "objetivo",
    watcher.TARGET_2: "objetivo",
    watcher.TIME: "tiempo",
    watcher.HORIZON: "tiempo",
    watcher.EARNINGS: "resultados",
}


def _alert(session: Session, alert_id: int) -> models.Alert:
    alert = session.get(models.Alert, alert_id)
    if alert is None:
        raise NotFound(f"no existe la alerta {alert_id}")
    if alert.resolved_at is not None:
        raise ConfirmationError(f"la alerta {alert_id} ya fue resuelta: {alert.resolution}")
    return alert


def _settle_alert_state(session: Session, position: models.Position, *, actor: str) -> None:
    """A position leaves SELL_ALERT once no sell alert is waiting for an answer."""
    if position.state != "SELL_ALERT":
        return
    waiting = [
        a for a in watcher.open_alerts(session, position.id) if watcher.RULES[a.rule].kind == "sell"
    ]
    if waiting:
        return
    initial = (position.tracking or {}).get("initial_shares") or position.qty_open
    back_to = "PARTIALLY_CLOSED" if position.qty_open < initial - 1e-9 else "OPEN"
    states.transition(
        session, "position", position, back_to, actor=actor, reason="alertas de venta resueltas"
    )


def _resolve(session: Session, alert: models.Alert, resolution: str) -> None:
    alert.resolved_at, alert.resolution = clock.now(), resolution
    if alert.rule == watcher.TARGET_1:
        # Answered once, the first target does not ask again.
        position = session.get(models.Position, alert.position_id)
        position.tracking = {**(position.tracking or {}), "target_1_done": True}
    session.flush()


def confirm_sold(
    session: Session,
    position_id: int,
    *,
    price: float,
    qty: float,
    filled_at: datetime | None = None,
    commission: float = 0.0,
    reason: str | None = None,
    note: str | None = None,
    alert_id: int | None = None,
) -> models.Position:
    """The user sold part or all of a real position. ``alert_id`` is the alert this
    sale answers, if any; closing the position resolves all of its alerts."""
    position = session.get(models.Position, position_id)
    if position is None or position.is_shadow or is_voided(position):
        raise NotFound(f"no existe la posición real {position_id}")
    if not price > 0 or not qty > 0 or commission < 0:
        raise ConfirmationError("el precio y la cantidad deben ser mayores a cero")
    if position.state == "CLOSED":
        # Selling what the system holds as already sold: reality and the database
        # disagree. The state machine refuses it and the system goes to PRECAUCIÓN.
        states.transition(session, "position", position, "CLOSED", actor="user", reason="Vendí")
    if qty > position.qty_open + 1e-9:
        raise ConfirmationError(
            f"quieres registrar la venta de {qty:g} acciones y solo quedan {position.qty_open:g}"
        )
    alert = _alert(session, alert_id) if alert_id is not None else None
    if alert is not None and alert.position_id != position.id:
        raise ConfirmationError(f"la alerta {alert_id} no es de esta posición")
    if reason is None and alert is not None:
        reason = _SALE_REASONS.get(alert.rule, watcher.RULES[alert.rule].name.lower())
    filled_at = clock.ensure_utc(filled_at) if filled_at is not None else clock.now()
    remaining = round(position.qty_open - qty, 6)
    closing = remaining <= 1e-9
    what = f"Vendí {qty:g} a ${price:,.2f}" + (f" ({reason})" if reason else "")

    answered = watcher.open_alerts(session, position.id) if closing else ([alert] if alert else [])
    for item in answered:
        _resolve(session, item, f"vendido: {qty:g} a ${price:,.2f}")
    if closing:
        states.transition(session, "position", position, "CLOSED", actor="user", reason=what)
    else:
        still_asking = any(
            watcher.RULES[a.rule].kind == "sell" for a in watcher.open_alerts(session, position.id)
        )
        waits_in_alert = position.state == "SELL_ALERT" and still_asking
        if not waits_in_alert and position.state != "PARTIALLY_CLOSED":
            states.transition(
                session, "position", position, "PARTIALLY_CLOSED", actor="user", reason=what
            )
    fill = models.Fill(
        position_id=position.id,
        side="sell",
        price=price,
        qty=qty,
        filled_at=filled_at,
        commission=commission,
        origin="user",
    )
    session.add(fill)
    position.qty_open = 0.0 if closing else remaining
    if closing:
        position.closed_at = filled_at
    session.flush()

    # The real result: R with the price actually paid, and dollars net of commissions.
    fills = fills_of(session, [position.id]).get(position.id, [])
    bought = sum(f.qty for f in fills if f.side == "buy")
    buy_commissions = sum(f.commission for f in fills if f.side == "buy")
    data = {
        "fill_id": fill.id,
        "ticker": position.ticker,
        "price": price,
        "qty": qty,
        "commission": commission,
        "reason": reason,
        "alert_id": alert.id if alert is not None else None,
        "day": clock.market_date(filled_at).isoformat(),
        "filled_at": filled_at.isoformat(),
        "r_multiple": round((price - position.avg_price) / position.initial_r, 3),
        "net_usd": round(
            (price - position.avg_price) * qty - commission - buy_commissions * qty / bought, 2
        ),
    }
    if closing:
        total = net_result(fills)
        data["position_net_usd"] = round(total, 2)
        data["position_r"] = round(total / (position.initial_r * bought), 3)
    session.add(
        models.Confirmation(
            kind="sold",
            idea_id=position.idea_id,
            position_id=position.id,
            alert_id=alert.id if alert is not None else None,
            data=data,
            note=note,
            created_at=clock.now(),
        )
    )
    # After the first target the rest is protected at the entry price: say so.
    suggested = (alert.data or {}).get("new_stop") if alert is not None else None
    after_first_target = not closing and alert is not None and alert.rule == watcher.TARGET_1
    if after_first_target and suggested and suggested > position.stop:
        watcher.raise_alert(
            session,
            position,
            watcher.Signal(
                watcher.TRAILING,
                f"Sube tu stop a ${suggested:,.2f}",
                "ya vendiste en el objetivo 1: el resto se protege en tu precio de entrada",
                f"tu stop sigue en ${position.stop:,.2f} y puedes devolver la ganancia",
                new_stop=suggested,
            ),
            price=price,
            source="tu venta",
            at=clock.now(),
        )
    return position


# --- "Ya subí el stop" and "Ignorar" -----------------------------------------------------------


def confirm_stop_raised(
    session: Session,
    position_id: int,
    *,
    new_stop: float | None = None,
    alert_id: int | None = None,
    note: str | None = None,
) -> models.Position:
    """The user moved their stop up. Without ``new_stop`` the level the alert
    suggested is used. The stop never moves down."""
    position = session.get(models.Position, position_id)
    if position is None or position.is_shadow or is_voided(position) or position.state == "CLOSED":
        raise NotFound(f"no existe la posición real abierta {position_id}")
    alert = _alert(session, alert_id) if alert_id is not None else None
    if alert is not None and alert.position_id != position.id:
        raise ConfirmationError(f"la alerta {alert_id} no es de esta posición")
    if new_stop is None and alert is not None:
        new_stop = (alert.data or {}).get("new_stop")
    if new_stop is None:
        raise ConfirmationError("falta el nuevo precio del stop")
    if new_stop <= position.stop:
        raise ConfirmationError(
            f"el stop solo sube: el actual es ${position.stop:,.2f} y quieres ponerlo en "
            f"${new_stop:,.2f}"
        )
    before = position.stop
    position.stop = float(new_stop)
    session.add(
        models.Confirmation(
            kind="stop_raised",
            idea_id=position.idea_id,
            position_id=position.id,
            alert_id=alert.id if alert is not None else None,
            data={"ticker": position.ticker, "from": before, "to": position.stop},
            note=note,
            created_at=clock.now(),
        )
    )
    if alert is not None and watcher.RULES[alert.rule].kind in ("stop", "either"):
        _resolve(session, alert, f"stop subido a ${position.stop:,.2f}")
        _settle_alert_state(session, position, actor="user")
    return position


def ignore_alert(
    session: Session, alert_id: int, *, reason: str | None = None, note: str | None = None
) -> models.Alert:
    """The user dismisses an alert. The position stays as it is; the same rule does
    not ask again today (a stop still breached tomorrow asks again)."""
    alert = _alert(session, alert_id)
    position = session.get(models.Position, alert.position_id)
    tracking = dict(position.tracking or {})
    tracking["ignored"] = {
        **(tracking.get("ignored") or {}),
        alert.rule: clock.market_date(clock.now()).isoformat(),
    }
    position.tracking = tracking
    _resolve(session, alert, f"ignorada: {reason}" if reason else "ignorada")
    _settle_alert_state(session, position, actor="user")
    session.add(
        models.Confirmation(
            kind="declined",
            idea_id=position.idea_id,
            position_id=position.id,
            alert_id=alert.id,
            data={"ticker": position.ticker, "rule": alert.rule, "reason": reason},
            note=note,
            created_at=clock.now(),
        )
    )
    return alert


# --- shadows -----------------------------------------------------------------------------------


def _session_time(day: date, *, close: bool) -> datetime:
    trading = market_calendar.get_session(day)
    if trading is None:
        return clock.market_time(day, time(16, 0) if close else time(9, 30))
    return trading.close if close else trading.open


def _open_shadow(
    session: Session, idea: models.Idea, price: float, day: date, config: AppConfig
) -> models.Position:
    position = models.Position(
        idea_id=idea.id,
        is_shadow=True,
        ticker=idea.ticker,
        qty_open=idea.shares,
        avg_price=price,
        stop=idea.stop,
        targets=list(idea.targets),
        initial_r=price - idea.stop,
        state="OPEN",
        opened_at=_session_time(day, close=False),
        tracking=_tracking(idea, price, idea.shares),
    )
    session.add(position)
    session.flush()
    states.record_creation(
        session, "position", position, actor="system", reason="sombra: entrada teórica"
    )
    session.add(
        models.Fill(
            position_id=position.id,
            side="buy",
            price=price,
            qty=idea.shares,
            filled_at=position.opened_at,
            commission=exits.trading_cost(price, idea.shares, config.costs),
            origin="virtual",
        )
    )
    return position


def _as_shadow(position: models.Position) -> exits.ShadowPosition:
    """The in-memory position the exit rules work on, restored from the database."""
    tracking = position.tracking or {}
    shadow = exits.ShadowPosition(
        ticker=position.ticker,
        setup=tracking.get("setup"),
        sector_etf=tracking.get("sector_etf"),
        entry_day=clock.market_date(position.opened_at),
        entry_price=position.avg_price,
        shares=tracking.get("initial_shares", position.qty_open),
        stop=position.avg_price - position.initial_r,
        targets=tuple(position.targets),
        atr=tracking.get("atr") or position.initial_r,
        horizon_days=tracking.get("horizon_days", DEFAULT_HORIZON_DAYS),
        score=tracking.get("score") or 0.0,
    )
    shadow.shares_open = position.qty_open
    shadow.stop = position.stop
    shadow.highest = tracking.get("highest", position.avg_price)
    shadow.days_held = tracking.get("days_held", 0)
    shadow.target_1_done = tracking.get("target_1_done", False)
    shadow.last_price = tracking.get("last_price", position.avg_price)
    return shadow


def _apply_bar(
    session: Session,
    position: models.Position,
    bar: exits.Bar,
    config: AppConfig,
    *,
    entry_day: bool = False,
    earnings_before_next_open: bool = False,
    regime: str = "",
) -> list[exits.Fill]:
    """Run the exit rules on one day's bar for a shadow position and store the result."""
    shadow = _as_shadow(position)
    fills = exits.apply_bar(
        shadow,
        bar,
        config.exits,
        config.costs,
        entry_day=entry_day,
        earnings_before_next_open=earnings_before_next_open,
        regime=regime,
        fractional_shares=config.capital.fractional_shares,
    )
    closing_time = _session_time(bar.day, close=True)
    for fill in fills:
        session.add(
            models.Fill(
                position_id=position.id,
                side="sell",
                price=fill.price,
                qty=fill.shares,
                filled_at=closing_time,
                commission=fill.cost,
                origin="virtual",
            )
        )
    if fills:
        target = "CLOSED" if not shadow.is_open else "PARTIALLY_CLOSED"
        if position.state != target:
            states.transition(
                session, "position", position, target, actor="system", reason=fills[-1].reason
            )
        if not shadow.is_open:
            position.closed_at = closing_time
    position.qty_open = shadow.shares_open
    position.stop = shadow.stop
    position.tracking = {
        **(position.tracking or {}),
        "highest": shadow.highest,
        "days_held": shadow.days_held,
        "target_1_done": shadow.target_1_done,
        "last_price": shadow.last_price,
        "exit_reason": shadow.exit_reason,
    }
    return fills


def settle_day(
    session: Session, config: AppConfig, day: date, bars: Mapping[str, exits.Bar]
) -> dict[str, list[str]]:
    """The close of ``day`` for its plan: ideas nobody answered expire, and every
    purchase idea that was not bought becomes a shadow if the price reached its
    entry zone, or "not fillable" if it did not. A ticker that already has an open
    shadow does not get a second one: the same idea repeated day after day would
    count several times in the statistics. Returns tickers by outcome."""
    outcome: dict[str, list[str]] = {
        "expired": [],
        "shadow": [],
        "not_fillable": [],
        "repeated": [],
    }
    shadowed = set(
        session.scalars(
            select(models.Position.ticker).where(
                models.Position.is_shadow.is_(True), models.Position.state != "CLOSED"
            )
        )
    )
    for idea in todays_ideas(session, day):
        if idea.state == "PROPOSED":
            why = (
                "sin respuesta al cierre"
                if idea.verdict == "COMPRAR"
                else "no era una compra propuesta"
            )
            states.transition(session, "idea", idea, "EXPIRED", actor="system", reason=why)
            outcome["expired"].append(idea.ticker)
        if idea.state not in ("EXPIRED", "DECLINED") or idea.verdict != "COMPRAR":
            continue
        if idea.ticker in shadowed:
            states.transition(
                session,
                "idea",
                idea,
                "NOT_FILLABLE",
                actor="system",
                reason=f"no se abre otra sombra: ya hay una abierta de {idea.ticker}",
            )
            outcome["repeated"].append(idea.ticker)
            continue
        bar = bars.get(idea.ticker)
        if bar is None or not idea.shares or idea.stop is None:
            price, why = None, "no ejecutable: sin precio ese día"
        else:
            price, why = exits.theoretical_entry(idea.entry_max, idea.stop, bar)
        if price is None:
            states.transition(session, "idea", idea, "NOT_FILLABLE", actor="system", reason=why)
            outcome["not_fillable"].append(idea.ticker)
            continue
        states.transition(
            session,
            "idea",
            idea,
            "SHADOW",
            actor="system",
            reason=f"sombra con entrada teórica en ${price:,.2f}",
        )
        position = _open_shadow(session, idea, price, day, config)
        # On the entry day only the stop is checked.
        _apply_bar(session, position, bar, config, entry_day=True)
        if position.state != "CLOSED":
            shadowed.add(idea.ticker)
        outcome["shadow"].append(idea.ticker)
    return outcome


def update_shadows(
    session: Session,
    config: AppConfig,
    day: date,
    bars: Mapping[str, exits.Bar],
    *,
    regime: str = "",
    reports_before_next_open: Callable[[str], bool] = lambda ticker: False,
) -> dict[str, list[str]]:
    """Follow every open shadow through ``day`` with the exit rules. Returns what
    each one did: ticker -> reasons of its sales."""
    activity: dict[str, list[str]] = {}
    shadows = session.scalars(
        select(models.Position)
        .where(models.Position.is_shadow.is_(True), models.Position.state != "CLOSED")
        .order_by(models.Position.id)
    )
    for position in shadows:
        bar = bars.get(position.ticker)
        if bar is None or clock.market_date(position.opened_at) >= day:
            continue  # no price that day, or opened that same day (already handled)
        fills = _apply_bar(
            session,
            position,
            bar,
            config,
            earnings_before_next_open=reports_before_next_open(position.ticker),
            regime=regime,
        )
        if fills:
            activity[position.ticker] = [fill.reason for fill in fills]
    return activity
