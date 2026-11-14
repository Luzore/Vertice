"""Node 9: the watcher (ROADMAP section 12).

Watches the real positions and says when to sell, with a concrete reason. Its
rules are code: they work with the AI down or out of quota (principle 6). It
never sells anything; it raises an alert and waits for the user to say what they
did (principle 1).

Everything the watcher needs to remember lives in the database: the stop, the
highest price seen, whether the first target was already handled, and the alerts
themselves. A restart loses nothing.

* One alert per position and rule until it is resolved; the database refuses a
  second one.
* The stop only moves up, and only when the user says they moved it.
* The shadows follow the same rules with virtual fills (``engine.exits``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import clock, market_calendar
from core.logging import get_logger
from core.settings import AppConfig, ExitsConfig
from db import models
from engine import account, states

log = get_logger(__name__)

RuleKind = Literal["sell", "stop", "either", "suggest"]

STOP = "stop"
SEVERE_NEWS = "noticia_grave"
TARGET_1 = "objetivo_1"
TRAILING = "stop_movil"
TARGET_2 = "objetivo_2"
EARNINGS = "resultados"
THESIS = "tesis_invalidada"
REGIME = "regimen"
TIME = "tiempo"
HORIZON = "horizonte"
ROTATION = "rotacion"


@dataclass(frozen=True)
class RuleSpec:
    number: int
    key: str
    name: str
    urgency: str
    # "sell" alerts put the position in SELL_ALERT; "stop" ones ask to raise the stop;
    # "either" offers both; "suggest" is only a proposal.
    kind: RuleKind


RULES: dict[str, RuleSpec] = {
    spec.key: spec
    for spec in (
        RuleSpec(1, STOP, "Stop", "ALTA", "sell"),
        RuleSpec(2, SEVERE_NEWS, "Noticia grave", "ALTA", "sell"),
        RuleSpec(3, TARGET_1, "Objetivo 1", "MEDIA", "sell"),
        RuleSpec(4, TRAILING, "Stop móvil", "BAJA", "stop"),
        RuleSpec(5, TARGET_2, "Objetivo 2", "MEDIA", "sell"),
        RuleSpec(6, EARNINGS, "Resultados", "MEDIA", "sell"),
        RuleSpec(7, THESIS, "Tesis invalidada", "MEDIA", "sell"),
        RuleSpec(8, REGIME, "Régimen", "MEDIA", "either"),
        RuleSpec(9, TIME, "Tiempo", "BAJA", "sell"),
        RuleSpec(10, HORIZON, "Horizonte", "BAJA", "sell"),
        RuleSpec(11, ROTATION, "Rotación", "BAJA", "suggest"),
    )
}

MIN_EVALUATION_INTERVAL_S = 10
# A trailing stop suggestion must improve the stop by at least this share of an ATR.
MIN_STOP_IMPROVEMENT_ATR = 0.25
_EPSILON = 1e-9
_INVALIDATION = re.compile(
    r"(?:debajo|bajo|menor|inferior)\s+(?:de|a|del)?\s*\$?\s*(\d+(?:[.,]\d+)?)", re.I
)


@dataclass(frozen=True)
class Tick:
    """A price of one ticker at one moment."""

    ticker: str
    price: float
    ts: datetime
    # Where the price comes from. IEX is an approximation of the consolidated price.
    source: str = "alpaca:iex"
    # True for the first price of the session (a gap below the stop is rule 1 too).
    is_open: bool = False
    # Extremes since the last evaluation, when the feed aggregated several prices.
    high: float | None = None
    low: float | None = None


@dataclass(frozen=True)
class PositionView:
    """What the rules look at for one real position."""

    id: int
    ticker: str
    qty_open: float
    initial_shares: float
    entry: float
    stop: float
    targets: tuple[float, ...]
    initial_r: float
    atr: float
    highest: float
    target_1_done: bool
    horizon_days: int
    # Trading days since the entry day.
    days_held: int

    def r_multiple(self, price: float) -> float:
        return (price - self.entry) / self.initial_r

    @property
    def best_r(self) -> float:
        return self.r_multiple(self.highest)


@dataclass(frozen=True)
class Signal:
    """A rule that fired: what to do and why."""

    rule: str
    action: str
    explanation: str
    # What happens if the user does nothing.
    consequence: str
    sell_qty: float | None = None
    new_stop: float | None = None


def view_of(position: models.Position, today: date) -> PositionView:
    tracking = position.tracking or {}
    opened_on = clock.market_date(position.opened_at)
    return PositionView(
        id=position.id,
        ticker=position.ticker,
        qty_open=position.qty_open,
        initial_shares=tracking.get("initial_shares") or position.qty_open,
        entry=position.avg_price,
        stop=position.stop,
        targets=tuple(position.targets or ()),
        initial_r=position.initial_r,
        atr=tracking.get("atr") or position.initial_r,
        highest=max(tracking.get("highest") or position.avg_price, position.avg_price),
        target_1_done=bool(tracking.get("target_1_done")),
        horizon_days=int(tracking.get("horizon_days") or 10),
        days_held=max(0, market_calendar.count_trading_days(opened_on, today) - 1),
    )


def _half(view: PositionView, exits: ExitsConfig, fractional: bool) -> float:
    part = view.initial_shares * exits.target1_sell_pct / 100
    if not fractional:
        part = math.floor(part + 1e-9) or view.qty_open
    return min(part, view.qty_open)


def tick_signals(
    view: PositionView,
    tick: Tick,
    exits: ExitsConfig,
    *,
    regime: str = "",
    fractional_shares: bool = False,
) -> list[Signal]:
    """The rules that depend only on the price: stop, targets, trailing stop, regime.

    ``view.highest`` must already include this tick.
    """
    price = tick.price
    low = tick.low if tick.low is not None else price
    high = tick.high if tick.high is not None else price
    signals: list[Signal] = []

    # Rule 1: the stop, or an open below it.
    if low <= view.stop:
        if tick.is_open and price <= view.stop:
            why = f"abrió en ${price:,.2f}, debajo de tu stop de ${view.stop:,.2f}"
        else:
            why = f"el precio tocó ${low:,.2f} y tu stop está en ${view.stop:,.2f}"
        return [
            Signal(
                STOP,
                "Vender todo ya",
                why,
                "la pérdida deja de estar limitada: el plan era salir aquí",
                sell_qty=view.qty_open,
            )
        ]

    # Rule 5: the second target sells the rest.
    if len(view.targets) > 1 and high >= view.targets[1]:
        signals.append(
            Signal(
                TARGET_2,
                "Vender el resto",
                f"llegó al objetivo 2 de ${view.targets[1]:,.2f} "
                f"(+{view.r_multiple(view.targets[1]):.1f}R)",
                "la ganancia sigue expuesta al mercado; el plan terminaba aquí",
                sell_qty=view.qty_open,
            )
        )
    # Rule 3: the first target sells part and protects the rest.
    elif view.targets and not view.target_1_done and high >= view.targets[0]:
        part = _half(view, exits, fractional_shares)
        protect = exits.move_stop_to_breakeven_after_t1 and view.entry > view.stop
        signals.append(
            Signal(
                TARGET_1,
                f"Vender {part:g} acciones ({exits.target1_sell_pct:g}%)"
                + (
                    f" y subir el stop a ${view.entry:,.2f}, tu precio de entrada"
                    if protect
                    else ""
                ),
                f"llegó al objetivo 1 de ${view.targets[0]:,.2f} "
                f"(+{view.r_multiple(view.targets[0]):.1f}R)",
                "puedes devolver la ganancia si el precio se regresa",
                sell_qty=part,
                new_stop=view.entry if protect else None,
            )
        )

    # Rule 4: trailing stop once the trade has gone far enough.
    if view.best_r >= exits.trailing_start_r - _EPSILON:
        level = round(view.highest - exits.trailing_atr_mult * view.atr, 2)
        if level >= view.stop + MIN_STOP_IMPROVEMENT_ATR * view.atr and level < price:
            signals.append(
                Signal(
                    TRAILING,
                    f"Sube tu stop a ${level:,.2f}",
                    f"el máximo desde la entrada es ${view.highest:,.2f} "
                    f"(+{view.best_r:.1f}R): el stop sube a {exits.trailing_atr_mult:g} ATR "
                    "debajo de ese máximo",
                    f"tu stop sigue en ${view.stop:,.2f} y protege menos ganancia",
                    new_stop=level,
                )
            )

    # Rule 8: in a high-risk market, tighten what is not yet +1R.
    if regime == "RIESGO_ALTO" and view.r_multiple(price) < 1:
        level = round(price - view.atr, 2)
        if level >= view.stop + MIN_STOP_IMPROVEMENT_ATR * view.atr:
            signals.append(
                Signal(
                    REGIME,
                    f"Sube tu stop a ${level:,.2f} (1 ATR) o vende la mitad",
                    f"el mercado pasó a RIESGO ALTO y la posición va en "
                    f"{view.r_multiple(price):+.1f}R, debajo de +1R",
                    "quedas con el riesgo completo en un mercado que se complicó",
                    sell_qty=_half(view, exits, fractional_shares),
                    new_stop=level,
                )
            )
    return signals


def invalidation_level(text: str | None) -> float | None:
    """The price in an invalidation written as "cierre debajo de 48.30"; None when
    the condition is not numeric (then only the AI can judge it)."""
    if not text:
        return None
    match = _INVALIDATION.search(text)
    return float(match.group(1).replace(",", ".")) if match else None


def close_review_signals(
    view: PositionView,
    price: float,
    exits: ExitsConfig,
    *,
    reports_soon: bool = False,
    invalidation: str | None = None,
) -> list[Signal]:
    """The rules checked once a day before the close: earnings, invalidated thesis,
    time stop and horizon."""
    signals: list[Signal] = []
    # Rule 6: do not hold through an earnings report.
    if reports_soon and not exits.hold_through_earnings:
        signals.append(
            Signal(
                EARNINGS,
                "Vender antes del cierre de hoy",
                "la empresa reporta resultados antes de la próxima apertura",
                "mañana el precio puede abrir muy lejos de tu stop, en cualquier dirección",
                sell_qty=view.qty_open,
            )
        )
    # Rule 7: the numeric invalidation of the plan.
    level = invalidation_level(invalidation)
    if level is not None and price < level:
        signals.append(
            Signal(
                THESIS,
                "Vender",
                f"se cumplió la invalidación del plan: el precio (${price:,.2f}) está debajo "
                f"de ${level:,.2f}",
                "la razón por la que se compró ya no vale",
                sell_qty=view.qty_open,
            )
        )
    # Rule 9: time stop for a trade that goes nowhere.
    if view.days_held >= exits.time_stop_days and view.best_r < exits.time_stop_min_r - _EPSILON:
        signals.append(
            Signal(
                TIME,
                "Vender al cierre",
                f"lleva {view.days_held} días hábiles y nunca llegó a "
                f"+{exits.time_stop_min_r:g}R (máximo {view.best_r:+.1f}R)",
                "el dinero sigue ocupado en una idea que no avanza",
                sell_qty=view.qty_open,
            )
        )
    # Rule 10: the horizon of the idea.
    elif view.days_held >= view.horizon_days:
        signals.append(
            Signal(
                HORIZON,
                "Vender",
                f"se cumplió el horizonte de {view.horizon_days} días hábiles de la idea",
                "la idea era de corto plazo; seguir ya es otra operación",
                sell_qty=view.qty_open,
            )
        )
    return signals


# --- alerts in the database --------------------------------------------------------------------


def open_alert(session: Session, position_id: int, rule: str) -> models.Alert | None:
    return session.scalars(
        select(models.Alert).where(
            models.Alert.position_id == position_id,
            models.Alert.rule == rule,
            models.Alert.resolved_at.is_(None),
        )
    ).first()


def open_alerts(session: Session, position_id: int | None = None) -> list[models.Alert]:
    query = select(models.Alert).where(models.Alert.resolved_at.is_(None))
    if position_id is not None:
        query = query.where(models.Alert.position_id == position_id)
    return list(session.scalars(query.order_by(models.Alert.id)))


def _alert_data(
    position: models.Position, signal: Signal, price: float, source: str, at: datetime
) -> dict:
    spec = RULES[signal.rule]
    gain = (price - position.avg_price) * position.qty_open
    return {
        "ticker": position.ticker,
        "rule": spec.key,
        "rule_number": spec.number,
        "rule_name": spec.name,
        "kind": spec.kind,
        "action": signal.action,
        "explanation": signal.explanation,
        "consequence": signal.consequence,
        "price": price,
        "price_source": source,
        "price_at": at.isoformat(),
        "qty_open": position.qty_open,
        "pnl_usd": round(gain, 2),
        "pnl_pct": round((price / position.avg_price - 1) * 100, 2),
        "r_now": round((price - position.avg_price) / position.initial_r, 2),
        "sell_qty": signal.sell_qty,
        "new_stop": signal.new_stop,
        "stop": position.stop,
    }


def raise_alert(
    session: Session,
    position: models.Position,
    signal: Signal,
    *,
    price: float,
    source: str,
    at: datetime,
) -> models.Alert | None:
    """Create the alert of a rule that fired. Returns None when that position already
    has an unresolved alert of the rule (its data is refreshed) or when the user
    ignored that rule today."""
    spec = RULES[signal.rule]
    today = clock.market_date(at).isoformat()
    tracking = position.tracking or {}
    if (tracking.get("ignored") or {}).get(spec.key) == today:
        return None
    data = _alert_data(position, signal, price, source, at)
    existing = open_alert(session, position.id, spec.key)
    if existing is not None:
        previous = existing.data or {}
        # The suggested stop of an open alert only ever goes up.
        if previous.get("new_stop") and data.get("new_stop"):
            data["new_stop"] = max(previous["new_stop"], data["new_stop"])
            if data["new_stop"] != signal.new_stop:
                data["action"] = previous.get("action", data["action"])
        existing.data = data
        return None
    alert = models.Alert(
        position_id=position.id,
        rule=spec.key,
        urgency=spec.urgency,
        message=f"{position.ticker} · {spec.name}: {signal.explanation}",
        created_at=at,
        data=data,
    )
    try:
        with session.begin_nested():
            session.add(alert)
            session.flush()
    except IntegrityError:
        # Another process raised the same alert a moment ago: the database keeps one.
        return None
    if spec.kind == "sell" and position.state in ("OPEN", "PARTIALLY_CLOSED"):
        states.transition(
            session, "position", position, "SELL_ALERT", actor="system", reason=alert.message
        )
    log.info("alert_raised", ticker=position.ticker, rule=spec.key, urgency=spec.urgency)
    return alert


class Watcher:
    """Applies the exit rules to the real positions as prices arrive."""

    def __init__(
        self,
        engine: Engine,
        config: AppConfig,
        *,
        min_interval_s: float = MIN_EVALUATION_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._config = config
        self._min_interval_s = min_interval_s
        # Per ticker: time of the last evaluation and the extremes seen since then.
        # Losing this on a restart only means the next price is evaluated at once.
        self._last_evaluated: dict[str, datetime] = {}
        self._pending: dict[str, tuple[float, float]] = {}
        self.last_prices: dict[str, Tick] = {}

    def watched_tickers(self) -> list[str]:
        """Tickers of the open real positions: what the price feed must follow."""
        with states.guarded_session(self._engine) as session:
            return sorted({p.ticker for p in account.real_positions(session, open_only=True)})

    def on_tick(self, tick: Tick, *, regime: str = "") -> list[int]:
        """Evaluate the price rules for the positions of ``tick.ticker``, at most once
        every ten seconds per ticker. Returns the ids of the alerts just created."""
        high = tick.high if tick.high is not None else tick.price
        low = tick.low if tick.low is not None else tick.price
        seen_high, seen_low = self._pending.get(tick.ticker, (high, low))
        high, low = max(high, seen_high), min(low, seen_low)
        self.last_prices[tick.ticker] = tick
        last = self._last_evaluated.get(tick.ticker)
        if (
            last is not None
            and not tick.is_open
            and (tick.ts - last).total_seconds() < self._min_interval_s
        ):
            self._pending[tick.ticker] = (high, low)
            return []
        self._pending.pop(tick.ticker, None)
        self._last_evaluated[tick.ticker] = tick.ts
        merged = Tick(tick.ticker, tick.price, tick.ts, tick.source, tick.is_open, high, low)

        created: list[int] = []
        today = clock.market_date(tick.ts)
        with states.guarded_session(self._engine) as session:
            for position in account.real_positions(session, open_only=True):
                if position.ticker != tick.ticker:
                    continue
                tracking = dict(position.tracking or {})
                tracking["highest"] = max(tracking.get("highest") or position.avg_price, high)
                tracking["last_price"] = tick.price
                position.tracking = tracking
                signals = tick_signals(
                    view_of(position, today),
                    merged,
                    self._config.exits,
                    regime=regime,
                    fractional_shares=self._config.capital.fractional_shares,
                )
                for signal in signals:
                    alert = raise_alert(
                        session, position, signal, price=tick.price, source=tick.source, at=tick.ts
                    )
                    if alert is not None:
                        created.append(alert.id)
        return created

    def close_review(
        self,
        prices: Mapping[str, float],
        *,
        as_of: datetime | None = None,
        reports_soon: Iterable[str] = (),
        source: str = "alpaca:iex",
    ) -> list[int]:
        """The review before the close: earnings tomorrow, invalidated thesis, time
        stop and horizon. Returns the ids of the alerts just created."""
        as_of = as_of or clock.now()
        today = clock.market_date(as_of)
        reporting = set(reports_soon)
        created: list[int] = []
        with states.guarded_session(self._engine) as session:
            for position in account.real_positions(session, open_only=True):
                tracking = position.tracking or {}
                price = prices.get(position.ticker) or tracking.get("last_price")
                if price is None:
                    continue
                idea = session.get(models.Idea, position.idea_id)
                signals = close_review_signals(
                    view_of(position, today),
                    price,
                    self._config.exits,
                    reports_soon=position.ticker in reporting,
                    invalidation=idea.invalidation if idea is not None else None,
                )
                for signal in signals:
                    alert = raise_alert(
                        session, position, signal, price=price, source=source, at=as_of
                    )
                    if alert is not None:
                        created.append(alert.id)
        return created

    def suggest_rotations(self, rotations: Iterable[Mapping], *, as_of: datetime | None = None):
        """Rule 11: turn the plan's rotation proposals into low-urgency alerts on the
        position proposed for sale. Returns the ids of the alerts just created."""
        as_of = as_of or clock.now()
        created: list[int] = []
        with states.guarded_session(self._engine) as session:
            by_ticker = {p.ticker: p for p in account.real_positions(session, open_only=True)}
            for rotation in rotations:
                position = by_ticker.get(rotation["sell"])
                if position is None:
                    continue
                price = (position.tracking or {}).get("last_price") or position.avg_price
                signal = Signal(
                    ROTATION,
                    f"Considera vender {position.ticker} para comprar {rotation['buy']}",
                    rotation["text"],
                    "nada: es solo una propuesta y la decisión es tuya",
                    sell_qty=position.qty_open,
                )
                alert = raise_alert(
                    session, position, signal, price=price, source="último precio", at=as_of
                )
                if alert is not None:
                    created.append(alert.id)
        return created

    # --- notifications -------------------------------------------------------------------

    def due_notifications(self, *, as_of: datetime | None = None) -> list[int]:
        """Alerts to send now: the ones never sent, and the ALTA ones due for a repeat
        (every few minutes, a limited number of times)."""
        as_of = as_of or clock.now()
        notify = self._config.notify
        due: list[int] = []
        with states.guarded_session(self._engine) as session:
            for alert in open_alerts(session):
                repeat_due = (
                    alert.urgency == "ALTA"
                    and alert.notified_count <= notify.repeat_high_alerts_max
                    and alert.last_notified_at is not None
                    and (as_of - alert.last_notified_at).total_seconds()
                    >= notify.repeat_high_alerts_min * 60
                )
                if alert.notified_count == 0 or repeat_due:
                    due.append(alert.id)
        return due

    def mark_notified(self, alert_id: int, *, as_of: datetime | None = None) -> None:
        with states.guarded_session(self._engine) as session:
            alert = session.get(models.Alert, alert_id)
            if alert is not None:
                alert.notified_count += 1
                alert.last_notified_at = as_of or clock.now()
