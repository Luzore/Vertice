"""AI review of an open position (ROADMAP sections 9 and 12).

The AI looks at a position only when something happened: news, a strong move, the
price near the stop or a target, or the review before the close; and at most once
a day for each of those reasons. It answers MANTENER, VENDER or SUBIR_STOP.

It is an opinion on top of the rules, never instead of them (principle 6):

* without quota, or if the AI fails, nothing changes: the stop, the targets and the
  time rules keep firing on their own;
* it cannot lower a stop, and a stop it proposes must be one of the levels listed
  in its dossier (principle 3), above the current stop and below the price;
* what it says becomes an alert for the user, never an action.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from core import clock
from core.settings import AppConfig, ExitsConfig
from db import models
from engine import watcher
from engine.ai_client import TASK_POSITION_REVIEW
from engine.ai_gateway import AIGateway
from engine.ai_validation import TOLERANCE_PCT
from engine.dossier import HEADLINE_LIMIT, Dossier
from engine.levels import NamedLevel
from engine.schemas import NewsItem
from engine.watcher import PositionView, Signal

NEWS, MOVE, NEAR_STOP, NEAR_TARGET, CLOSE = (
    "noticia nueva",
    "movimiento fuerte",
    "cerca del stop",
    "cerca del objetivo",
    "revisión de cierre",
)
MOVE_ATR = 1.5
NEAR_ATR = 0.25


@dataclass(frozen=True)
class Advice:
    action: str  # MANTENER, VENDER or SUBIR_STOP
    reason: str
    severe_news_confirmed: bool
    new_stop: float | None
    confidence: float


def due_triggers(
    view: PositionView,
    price: float,
    *,
    previous_close: float | None = None,
    has_news: bool = False,
    closing: bool = False,
) -> list[str]:
    """The reasons there are, right now, to ask the AI about a position."""
    reasons = []
    if has_news:
        reasons.append(NEWS)
    if previous_close and abs(price - previous_close) >= MOVE_ATR * view.atr:
        reasons.append(MOVE)
    if price - view.stop <= NEAR_ATR * view.atr:
        reasons.append(NEAR_STOP)
    pending = [t for t in view.targets if t > price]
    if pending and pending[0] - price <= NEAR_ATR * view.atr:
        reasons.append(NEAR_TARGET)
    if closing:
        reasons.append(CLOSE)
    return reasons


def valid_stops(view: PositionView, price: float, exits: ExitsConfig) -> list[NamedLevel]:
    """Where the stop may be raised to: always above the current one and below the price."""
    candidates = [
        NamedLevel(round(view.entry, 2), "tu precio de entrada"),
        NamedLevel(
            round(view.highest - exits.trailing_atr_mult * view.atr, 2),
            f"{exits.trailing_atr_mult:g} ATR debajo del máximo desde la entrada",
        ),
        NamedLevel(round(price - view.atr, 2), "1 ATR debajo del precio actual"),
    ]
    seen: dict[float, NamedLevel] = {}
    for level in candidates:
        if view.stop < level.price < price:
            seen.setdefault(level.price, level)
    return sorted(seen.values(), key=lambda level: level.price)


def build_review_dossier(
    position: models.Position,
    idea: models.Idea | None,
    view: PositionView,
    *,
    price: float,
    at: datetime,
    trigger: str,
    news: Sequence[NewsItem] = (),
    stops: Sequence[NamedLevel] = (),
) -> Dossier:
    block = {
        "ticker": position.ticker,
        "motivo_de_la_revision": trigger,
        "posicion": {
            "acciones": position.qty_open,
            "precio_de_entrada": round(view.entry, 2),
            "precio_actual": price,
            "stop_actual": view.stop,
            "objetivos": list(view.targets),
            "resultado_en_r": round(view.r_multiple(price), 2),
            "maximo_desde_la_entrada": round(view.highest, 2),
            "mejor_resultado_en_r": round(view.best_r, 2),
            "dias_habiles": view.days_held,
            "horizonte_dias": view.horizon_days,
            "atr": round(view.atr, 2),
        },
        "plan_original": {
            "setup": idea.setup if idea is not None else None,
            "tesis": idea.thesis if idea is not None else None,
            "invalidacion": idea.invalidation if idea is not None else None,
            "riesgos": (idea.risks or []) if idea is not None else [],
        },
        "niveles_validos": [{"precio": s.price, "nivel": s.name} for s in stops],
        "noticias": [
            {
                "fecha": f"{clock.to_market(item.published_at):%Y-%m-%d %H:%M} NY",
                "fuente": item.source,
                "titular": item.headline[:HEADLINE_LIMIT],
                "sentimiento": item.sentiment,
                **({"categoria": item.category} if item.category else {}),
                **({"grave": True} if item.severe else {}),
            }
            for item in sorted(news, key=lambda n: n.published_at, reverse=True)[:5]
        ],
    }
    body = json.dumps(block, ensure_ascii=False, indent=1)
    text = (
        f"# Revisión de posición · {position.ticker}\n\n"
        f"Hora: {clock.dual_label(at)}\n\n```json\n{body}\n```\n"
    )
    return Dossier(text, hashlib.sha256(body.encode("utf-8")).hexdigest(), (position.ticker,))


def _validated_stop(proposed: float | None, stops: Sequence[NamedLevel]) -> float | None:
    """The proposed stop if it is one of the valid levels (within tolerance), else None."""
    if proposed is None or not stops:
        return None
    nearest = min(stops, key=lambda level: abs(level.price - proposed))
    if abs(proposed - nearest.price) <= nearest.price * TOLERANCE_PCT / 100:
        return nearest.price
    return None


def review_position(
    session: Session,
    gateway: AIGateway,
    config: AppConfig,
    position: models.Position,
    *,
    price: float,
    at: datetime,
    trigger: str,
    news: Sequence[NewsItem] = (),
) -> Advice | None:
    """Ask the AI about a position, at most once a day per trigger. Returns None when
    it was already asked today, or when the AI could not be used."""
    today = clock.market_date(at)
    tracking = dict(position.tracking or {})
    asked = dict(tracking.get("ai_reviews") or {})
    if asked.get(trigger) == today.isoformat():
        return None
    asked[trigger] = today.isoformat()
    tracking["ai_reviews"] = asked
    position.tracking = tracking

    view = watcher.view_of(position, today)
    stops = valid_stops(view, price, config.exits)
    idea = session.get(models.Idea, position.idea_id)
    dossier = build_review_dossier(
        position, idea, view, price=price, at=at, trigger=trigger, news=news, stops=stops
    )

    def same_ticker(output: dict) -> str | None:
        if str(output.get("ticker", "")).strip().upper() != position.ticker:
            return "la respuesta es de otro ticker"
        return None

    result = gateway.call(
        TASK_POSITION_REVIEW,
        dossier.text,
        fingerprint=dossier.fingerprint,
        web_search=trigger == NEWS,
        check=same_ticker,
    )
    if not result.ok:
        return None
    output = result.output
    action = str(output["accion"])
    new_stop = _validated_stop(output.get("nuevo_stop"), stops) if action == "SUBIR_STOP" else None
    if action == "SUBIR_STOP" and new_stop is None:
        action = "MANTENER"  # a stop that is not one of the valid levels is not suggested
    return Advice(
        action=action,
        reason=str(output.get("motivo", "")).strip(),
        severe_news_confirmed=bool(output.get("noticia_grave_confirmada")),
        new_stop=new_stop,
        confidence=float(output.get("confianza", 0.0)),
    )


def advice_signal(advice: Advice, view: PositionView) -> Signal | None:
    """The alert an AI review turns into, if any. MANTENER raises nothing."""
    if advice.action == "VENDER":
        rule = watcher.SEVERE_NEWS if advice.severe_news_confirmed else watcher.THESIS
        return Signal(
            rule,
            "Vender todo" if advice.severe_news_confirmed else "Vender",
            f"la IA recomienda salir: {advice.reason}",
            "la razón por la que se compró pudo haber dejado de valer",
            sell_qty=view.qty_open,
        )
    if advice.action == "SUBIR_STOP" and advice.new_stop is not None:
        return Signal(
            watcher.TRAILING,
            f"Sube tu stop a ${advice.new_stop:,.2f}",
            f"la IA sugiere proteger la posición: {advice.reason}",
            f"tu stop sigue en ${view.stop:,.2f}",
            new_stop=advice.new_stop,
        )
    return None


def severe_news_signal(view: PositionView, item: NewsItem, advice: Advice | None) -> Signal:
    """Rule 2. The rules found the headline severe, so the alert is raised whatever
    the AI thinks; its opinion is only added to the explanation."""
    if advice is None:
        opinion = "sin opinión de la IA"
    elif advice.severe_news_confirmed or advice.action == "VENDER":
        opinion = f"la IA lo confirma: {advice.reason}"
    else:
        opinion = f"la IA no lo considera grave ({advice.reason}), pero la regla manda"
    return Signal(
        watcher.SEVERE_NEWS,
        "Vender todo",
        f"noticia grave: «{item.headline[:HEADLINE_LIMIT]}» ({item.source}); {opinion}",
        "una noticia así puede abrir el precio muy lejos de tu stop",
        sell_qty=view.qty_open,
    )
