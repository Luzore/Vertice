"""Triggers of the VIGILAR ideas (ROADMAP sections 11 and 12).

A watched idea becomes a purchase when a five-minute candle closes in its buyable
range (from the bottom of the entry zone up to the no-chase price) and, at that
price, everything a purchase needs still holds: room, cash, no brake and an
expectancy of at least +0.15R. The size is worked out again with the account as it
is at that moment (principle 4).

Only ideas waiting for something a price can fix are considered: the ones that
opened too high, the ones the AI asked to wait on, and the ones short of
expectancy, cash or room. An idea held back by its score is not promoted by a
price.

The candles are built from the prices the watcher receives. They carry no volume,
so the "relative volume above 1.5" part of a breakout trigger is not checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from core import clock, market_calendar
from core.settings import AppConfig
from db import models
from engine import account, plan_store, risk
from engine.recommender import PROMOTABLE_WATCH_KINDS
from engine.risk_profile import profile_for
from engine.trade_plan import TradePlan
from engine.watcher import Tick

CANDLE = timedelta(minutes=5)
# A failed promotion is not tried again for this long.
RETRY_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class Candle:
    ticker: str
    start: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def end(self) -> datetime:
        return self.start + CANDLE


class CandleBuilder:
    """Builds five-minute candles from ticks. A candle is returned once a later
    price shows that its five minutes are over."""

    def __init__(self) -> None:
        self._forming: dict[str, Candle] = {}

    @staticmethod
    def _bucket(ts: datetime) -> datetime:
        return ts - timedelta(minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond)

    def add(self, tick: Tick) -> Candle | None:
        start = self._bucket(tick.ts)
        high = tick.high if tick.high is not None else tick.price
        low = tick.low if tick.low is not None else tick.price
        forming = self._forming.get(tick.ticker)
        if forming is not None and forming.start == start:
            self._forming[tick.ticker] = Candle(
                tick.ticker, start, forming.open, max(forming.high, high),
                min(forming.low, low), tick.price,
            )  # fmt: skip
            return None
        self._forming[tick.ticker] = Candle(tick.ticker, start, tick.price, high, low, tick.price)
        return forming if forming is not None and forming.start < start else None


def watched_ideas(session: Session, day: date) -> list[models.Idea]:
    """Today's VIGILAR ideas that a price can turn into a purchase."""
    return [
        idea
        for idea in plan_store.todays_ideas(session, day)
        if idea.verdict == "VIGILAR"
        and idea.state == "PROPOSED"
        and idea.stop is not None
        and idea.targets
        and (idea.details or {}).get("watch_kind") in PROMOTABLE_WATCH_KINDS
    ]


def trigger_met(idea: models.Idea, candle: Candle, config: AppConfig) -> bool:
    """A five-minute candle closed in the buyable range, after the first minutes of
    the session in which the plan does not enter."""
    session = market_calendar.get_session(clock.market_date(candle.start))
    if session is None:
        return False
    earliest = session.open + timedelta(minutes=config.entry.skip_first_minutes)
    if candle.end < earliest or candle.end > session.close:
        return False
    return idea.entry_min <= candle.close <= idea.no_chase_above


def try_promote(
    session: Session, config: AppConfig, idea: models.Idea, price: float, at: datetime
) -> tuple[bool, str]:
    """Turn a watched idea into a purchase at ``price`` if it qualifies now. Returns
    whether it did, and the reason either way (in Spanish)."""
    details = dict(idea.details or {})
    last_try = details.get("trigger_checked_at")
    if last_try and at - datetime.fromisoformat(last_try) < RETRY_AFTER:
        return False, "ya se revisó hace poco"
    details["trigger_checked_at"] = at.isoformat()
    idea.details = details

    def no(reason: str) -> tuple[bool, str]:
        return False, reason

    if config.system.mode != "recommend":
        return no("el sistema está en modo solo vigilar")
    if not idea.stop < price <= idea.no_chase_above:
        return no("el precio salió del rango de compra")
    profile = profile_for(config.system.aggressiveness)
    portfolio = account.load_portfolio(session, config, as_of=at, prices={idea.ticker: price})
    history = account.load_history(session)
    brake = account.current_brake(session, config, portfolio, history)
    if not brake.allows_buying:
        return no(f"freno automático: {brake.text}")
    if portfolio.holds(idea.ticker):
        return no("ya tienes una posición abierta en este ticker")
    # Purchases already proposed today and not answered yet keep their room and cash.
    pending = [
        other
        for other in plan_store.todays_ideas(session, idea.plan_date)
        if other.verdict == "COMPRAR" and other.state == "PROPOSED" and other.id != idea.id
    ]
    if len(portfolio.holdings) + len(pending) >= profile.max_positions:
        return no(f"sin espacio: el límite es de {profile.max_positions} posiciones")
    cash = portfolio.cash - sum(other.amount_usd or 0.0 for other in pending)

    levels = TradePlan(
        entry_min=price,
        entry_max=price,
        no_chase_above=idea.no_chase_above,
        reference=details.get("reference") or price,
        stop=idea.stop,
        stop_level=details.get("stop_level"),
        stop_level_name=details.get("stop_level_name"),
        targets=tuple(idea.targets),
        atr=details.get("atr") or (price - idea.stop),
    )
    outlook = risk.expectancy(levels, idea.prob_calibrated, profile.min_probability, config.costs)
    if not outlook.passes:
        return no(
            f"a ${price:,.2f} la expectativa es de {outlook.ev_r:+.2f}R; el mínimo es "
            f"+{risk.MIN_EV_R:.2f}R"
        )
    streak, _ = risk.streak_multiplier(history)
    sector = details.get("sector_etf")
    size = risk.position_size(
        levels,
        capital=portfolio.capital,
        cash=cash,
        profile=profile,
        config=config,
        conviction=risk.conviction_multiplier(idea.prob_calibrated, profile.min_probability),
        controls=details.get("size_multiplier") or 1.0,
        streak=streak,
        sector_value=portfolio.sector_value(sector) if sector else 0.0,
    )
    if size.shares <= 0:
        return no(
            "COMPRAR si liberas efectivo"
            if size.limited_by == "efectivo disponible"
            else "el tamaño permitido no alcanza para una acción"
        )

    reason = f"se cumplió el disparador: vela de 5 min cerró en ${price:,.2f}"
    session.add(
        models.StateTransition(
            entity=plan_store.VERDICT_AUDIT,
            entity_id=str(idea.id),
            from_state=idea.verdict,
            to_state="COMPRAR",
            at=at,
            actor="system",
            reason=reason,
        )
    )
    idea.verdict, idea.reason = "COMPRAR", None
    idea.shares, idea.amount_usd, idea.risk_usd = size.shares, size.amount, size.risk_usd
    idea.ev_r = round(outlook.ev_r, 4)
    idea.details = {
        **details,
        "watch_kind": None,
        "multipliers": size.multipliers,
        "limited_by": size.limited_by,
        "promoted": {"at": at.isoformat(), "price": price},
    }
    return True, reason
