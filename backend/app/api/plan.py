"""The plan of the day, the account and the manual confirmations.

The system never sends an order (principle 1). These endpoints only record what
the user says they did: "la compré", "no la compré", "vendí".
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from collectors.health import data_health
from core import clock, settings
from db import models
from db.session import get_engine, session_scope
from engine import account, confirmations, plan_store, states
from engine.ai_gateway import DatabaseLedger, budget_status

router = APIRouter()


class Purchase(BaseModel):
    price: float = Field(gt=0, description="Precio promedio al que compraste")
    qty: float = Field(gt=0, description="Acciones compradas")
    filled_at: datetime | None = Field(default=None, description="Hora de la compra (ahora)")
    commission: float = Field(default=0.0, ge=0, description="Comisión pagada, con IVA")
    note: str | None = None


class Decline(BaseModel):
    reason: str | None = Field(default=None, description="sin efectivo, no me convenció, ...")
    note: str | None = None


class Sale(BaseModel):
    price: float = Field(gt=0)
    qty: float = Field(gt=0)
    filled_at: datetime | None = None
    commission: float = Field(default=0.0, ge=0)
    reason: str | None = Field(default=None, description="stop, objetivo, tiempo, ...")
    note: str | None = None


class PurchaseCorrection(BaseModel):
    price: float | None = Field(default=None, gt=0)
    qty: float | None = Field(default=None, gt=0)
    filled_at: datetime | None = None
    commission: float | None = Field(default=None, ge=0)
    note: str | None = None


class Resolution(BaseModel):
    component: str
    note: str = ""


class Note(BaseModel):
    note: str = ""


def _position(position: models.Position, fills: list[models.Fill], last: float | None) -> dict:
    price = last if last is not None else position.avg_price
    return {
        "id": position.id,
        "idea_id": position.idea_id,
        "ticker": position.ticker,
        "is_shadow": position.is_shadow,
        "state": position.state,
        "qty_open": position.qty_open,
        "avg_price": position.avg_price,
        "stop": position.stop,
        "targets": position.targets,
        "initial_r": position.initial_r,
        "last_price": price,
        "r_now": (price - position.avg_price) / position.initial_r,
        "opened_at": position.opened_at.isoformat(),
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
        "realized_usd": account.net_result(fills) + position.qty_open * position.avg_price
        if any(fill.side == "sell" for fill in fills)
        else 0.0,
        "fills": [
            {
                "side": fill.side,
                "price": fill.price,
                "qty": fill.qty,
                "commission": fill.commission,
                "origin": fill.origin,
                "at": fill.filled_at.isoformat(),
            }
            for fill in fills
        ],
    }


@router.get("/plan")
def plan(day: date | None = None) -> dict:
    """The plan in force for ``day`` (today in New York by default)."""
    day = day or clock.market_date(clock.now())
    with session_scope(get_engine()) as session:
        data = plan_store.plan_as_dict(session, day)
    if data is None:
        raise HTTPException(404, f"no hay Plan del día para el {day.isoformat()}")
    return data


@router.get("/positions")
def positions(shadow: bool = False, include_closed: bool = False) -> dict:
    """Real positions (confirmed purchases), or the shadows with ``shadow=true``."""
    with session_scope(get_engine()) as session:
        query = select(models.Position).where(models.Position.is_shadow.is_(shadow))
        if not include_closed:
            query = query.where(models.Position.state != "CLOSED")
        rows = [
            p
            for p in session.scalars(query.order_by(models.Position.id))
            if not account.is_voided(p)
        ]
        fills = account.fills_of(session, (p.id for p in rows))
        prices = account.last_prices(session, (p.ticker for p in rows), clock.now())
        return {
            "positions": [_position(p, fills.get(p.id, []), prices.get(p.ticker)) for p in rows]
        }


@router.get("/account")
def account_summary() -> dict:
    """Cash, capital, result of the day and of the week, brake and AI budget."""
    config = settings.get_config()
    engine = get_engine()
    with session_scope(engine) as session:
        portfolio = account.load_portfolio(session, config)
        history = account.load_history(session)
        brake = account.current_brake(session, config, portfolio, history)
    day = clock.market_date(clock.now())
    start = clock.market_time(day, datetime.min.time())
    budget = budget_status(config.ai, DatabaseLedger(engine).since(start), day)
    return {
        "cash_usd": portfolio.cash,
        "capital_usd": portfolio.capital,
        "deposited_usd": config.capital.cash_usd,
        "day_pnl_pct": portfolio.day_pnl_pct,
        "week_pnl_pct": portfolio.week_pnl_pct,
        "open_positions": len(portfolio.holdings),
        "brake": {
            "state": brake.state,
            "reasons": list(brake.reasons),
            "active": list(brake.active),
        },
        "ai_budget": {"used": budget.used, "limit": budget.limit, "state": budget.state},
    }


@router.get("/status")
def system_status() -> dict:
    """The state the banner shows: NORMAL, PRECAUCION, SOLO_VIGILAR or PAUSA, and why."""
    config = settings.get_config()
    with session_scope(get_engine()) as session:
        health = data_health(session, config.data)
        incidents = states.open_incidents(session)
        portfolio = account.load_portfolio(session, config)
        brake = account.current_brake(session, config, portfolio, account.load_history(session))
        problems = [*health.problems, *(row.message for row in incidents)]
        if brake.state == "PAUSA":
            state = "PAUSA"
        elif incidents or health.state == "PRECAUCION":
            state = "PRECAUCION"
        elif brake.state == "SOLO_VIGILAR" or config.system.mode != "recommend":
            state = "SOLO_VIGILAR"
        else:
            state = "NORMAL"
        return {
            "state": state,
            "problems": problems,
            "incidents": [
                {
                    "component": row.component,
                    "message": row.message,
                    "since": row.updated_at.isoformat(),
                }
                for row in incidents
            ],
            "brake": {"state": brake.state, "reasons": list(brake.reasons)},
            "mode": config.system.mode,
        }


@router.post("/status/incidents/resolve")
def resolve_incident(body: Resolution) -> dict:
    with session_scope(get_engine()) as session:
        if not states.resolve_incident(session, body.component, body.note):
            raise HTTPException(404, f"no hay un incidente abierto llamado {body.component}")
    return {"resolved": body.component}


@router.post("/brakes/{name}/lift")
def lift_brake(name: str, body: Note | None = None) -> dict:
    """Reactivate purchases after the weekly brake ("week") or the pause ("drawdown")."""
    config = settings.get_config()
    with session_scope(get_engine()) as session:
        capital = account.load_portfolio(session, config).capital
        if not account.lift_brake(session, name, capital=capital, note=body.note if body else ""):
            raise HTTPException(404, f"el freno '{name}' no está activo")
    return {"lifted": name}


@router.post("/ideas/{idea_id}/bought")
def bought(idea_id: int, body: Purchase) -> dict:
    """ "La compré": creates the real position with the price actually paid."""
    with states.guarded_session(get_engine()) as session:
        position = confirmations.confirm_bought(session, idea_id, **body.model_dump())
        session.flush()
        fills = account.fills_of(session, [position.id]).get(position.id, [])
        return {"position": _position(position, fills, None)}


@router.post("/ideas/{idea_id}/declined")
def declined(idea_id: int, body: Decline | None = None) -> dict:
    """ "No la compré": the idea is followed as a shadow from the close."""
    body = body or Decline()
    with states.guarded_session(get_engine()) as session:
        idea = confirmations.confirm_declined(session, idea_id, **body.model_dump())
        return {"idea": plan_store.idea_as_dict(idea)}


@router.post("/positions/{position_id}/sold")
def sold(position_id: int, body: Sale) -> dict:
    """ "Vendí": a partial or total sale of a real position."""
    with states.guarded_session(get_engine()) as session:
        position = confirmations.confirm_sold(session, position_id, **body.model_dump())
        session.flush()
        fills = account.fills_of(session, [position.id]).get(position.id, [])
        return {"position": _position(position, fills, None)}


@router.post("/confirmations/{confirmation_id}/correct")
def correct(confirmation_id: int, body: PurchaseCorrection) -> dict:
    """Fix the price, quantity, time or commission of a purchase already confirmed."""
    with states.guarded_session(get_engine()) as session:
        position = confirmations.correct_purchase(session, confirmation_id, **body.model_dump())
        session.flush()
        fills = account.fills_of(session, [position.id]).get(position.id, [])
        return {"position": _position(position, fills, None)}


@router.get("/ideas/{idea_id}/history")
def idea_history(idea_id: int) -> dict:
    """Every state the idea went through and every confirmation, for the audit."""
    with session_scope(get_engine()) as session:
        if session.get(models.Idea, idea_id) is None:
            raise HTTPException(404, f"no existe la idea {idea_id}")
        transitions = session.scalars(
            select(models.StateTransition)
            .where(
                models.StateTransition.entity.in_(("idea", plan_store.VERDICT_AUDIT)),
                models.StateTransition.entity_id == str(idea_id),
            )
            .order_by(models.StateTransition.id)
        )
        confirmed = session.scalars(
            select(models.Confirmation)
            .where(models.Confirmation.idea_id == idea_id)
            .order_by(models.Confirmation.id)
        )
        return {
            "transitions": [
                {
                    "what": "veredicto" if t.entity == plan_store.VERDICT_AUDIT else "estado",
                    "from": t.from_state,
                    "to": t.to_state,
                    "at": t.at.isoformat(),
                    "actor": t.actor,
                    "reason": t.reason,
                }
                for t in transitions
            ],
            "confirmations": [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "corrects_id": c.corrects_id,
                    "data": c.data,
                    "note": c.note,
                    "at": c.created_at.isoformat(),
                }
                for c in confirmed
            ],
        }
