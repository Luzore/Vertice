"""What the dashboard shows, assembled from the database (ROADMAP section 14).

Every function here reads; none of them changes anything. The API serves their
results as they are, so each panel of the dashboard has exactly one place where
its data comes from.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from collectors.health import data_health
from core import clock, market_calendar, settings
from core.settings import AppConfig
from db import models
from engine import account, metrics, plan_store, risk, runtime_config, states
from engine import watcher as watching
from engine.ai_gateway import DatabaseLedger, budget_status
from engine.risk_profile import profile_for

STATE_LABELS = {
    "NORMAL": "NORMAL",
    "PRECAUCION": "PRECAUCIÓN",
    "SOLO_VIGILAR": "SOLO VIGILAR",
    "PAUSA": "PAUSA",
    "SIN_CUOTA_IA": "SIN CUOTA IA",
    "DATOS_CAIDOS": "DATOS CAÍDOS",
}
_TONES = {
    "NORMAL": "ok",
    "SOLO_VIGILAR": "warn",
    "SIN_CUOTA_IA": "warn",
    "PRECAUCION": "warn",
    "DATOS_CAIDOS": "bad",
    "PAUSA": "bad",
}
SOURCE_LABELS = {
    "alpaca": "Alpaca",
    "finnhub": "Finnhub",
    "fred": "FRED",
    "sec_edgar": "SEC EDGAR",
    "rss_news": "RSS de noticias",
    "massive": "Massive",
    "banxico": "Banxico",
    "yahoo": "Yahoo Finance",
    "telegram": "Telegram",
}
# Layer of the data flow -> the sources that feed it.
LAYERS = (
    ("mercado", "Mercado", ("alpaca",)),
    ("volatilidad", "Volatilidad", ("alpaca", "fred")),
    ("sentimiento", "Sentimiento", ("rss_news", "finnhub")),
    ("noticias", "Noticias", ("alpaca", "rss_news")),
    ("macro", "Macro", ("fred",)),
    ("calendario", "Calendario", ("fred", "finnhub")),
    ("eventos", "Eventos", ("sec_edgar", "finnhub")),
)
_SEVERITY = {"ok": 0, "unknown": 0, "slow": 1, "not_configured": 2, "stale": 3, "error": 4}


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


# --- system state ------------------------------------------------------------------------------


def _ai_budget(engine: Engine, config: AppConfig) -> dict:
    day = clock.market_date(clock.now())
    start = clock.market_time(day, time(0, 0))
    budget = budget_status(config.ai, DatabaseLedger(engine).since(start), day)
    return {
        "used": budget.used,
        "limit": budget.limit,
        "state": budget.state,
        "enabled": config.ai.enabled,
        "cost_usd": round(budget.cost_usd, 4),
    }


def system_state(session: Session, engine: Engine, config: AppConfig) -> dict:
    """The state the header and the banner show, with the reason in plain words and
    what to do about it."""
    now = clock.now()
    health = data_health(session, config.data)
    incidents = states.open_incidents(session)
    # The dashboard values the positions at the last price the watcher saw.
    live = {
        p.ticker: p.tracking["last_price"]
        for p in account.real_positions(session, open_only=True)
        if (p.tracking or {}).get("last_price")
    }
    portfolio = account.load_portfolio(session, config, prices=live)
    history = account.load_history(session)
    brake = account.current_brake(session, config, portfolio, history)
    failed = session.scalars(
        select(models.SystemStatus).where(
            models.SystemStatus.component.startswith("job/"),
            models.SystemStatus.status == "error",
        )
    ).all()
    feed = session.get(models.SystemStatus, "watcher/feed")
    feed_down = (
        feed is not None and feed.status != "ok" and market_calendar.market_status(now) == "open"
    )
    ai = _ai_budget(engine, config)

    problems = [*health.problems, *(row.message for row in incidents)]
    problems += [
        f"Falló la tarea {row.component.removeprefix('job/')}: "
        f"{(json.loads(row.message or '{}')).get('detail')}"
        for row in failed
    ]
    if feed_down:
        problems.append(f"Sin precios en vivo para el vigilante: {feed.message}")

    critical_down = any(
        s.critical and s.status in ("error", "stale", "not_configured") for s in health.sources
    )
    if brake.state == "PAUSA" or config.system.mode == "paused":
        state = "PAUSA"
    elif critical_down:
        state = "DATOS_CAIDOS"
    elif incidents or failed or feed_down or health.state == "PRECAUCION":
        state = "PRECAUCION"
    elif config.ai.enabled and ai["state"] == "SIN_CUOTA":
        state = "SIN_CUOTA_IA"
    elif brake.state == "SOLO_VIGILAR" or config.system.mode != "recommend":
        state = "SOLO_VIGILAR"
    else:
        state = "NORMAL"

    if state == "NORMAL":
        text = (
            "Operando con normalidad: vigila el mercado y propone compras cuando una idea "
            "supera todos los controles."
        )
        action = None
    elif state == "PAUSA":
        if config.system.mode == "paused":
            text = (
                "En pausa: tú detuviste el sistema. No propone compras; sigue cuidando tus "
                "posiciones."
            )
            action = "Para reanudar, cambia el modo a Recomendar."
        else:
            text = f"En pausa: {brake.text}."
            action = "Revisa métricas, lecciones y parámetros, y reactiva cuando termines."
    elif state == "DATOS_CAIDOS":
        first = health.problems[0] if health.problems else "una fuente crítica no responde"
        text = f"Datos caídos: {first}."
        action = (
            "No se proponen compras con datos incompletos. Revisa la key y el estado de esa "
            "fuente en Configuración."
        )
    elif state == "PRECAUCION":
        text = f"Precaución: {problems[0]}."
        action = (
            "Revisa el incidente y márcalo como resuelto cuando la base refleje lo que pasó "
            "de verdad."
            if incidents
            else "El sistema sigue funcionando con lo que tiene; revisa el detalle en Registro."
        )
    elif state == "SIN_CUOTA_IA":
        text = (
            f"Sin cuota de IA: se usaron {ai['used']} de {ai['limit']} consultas hoy. El plan sale "
            "con veredictos de reglas, marcados «sin IA»; el vigilante sigue funcionando."
        )
        action = "Mañana se restablece. Puedes subir el límite en Configuración."
    else:
        if config.system.mode != "recommend":
            text = "Solo vigilando: no propone compras nuevas; sigue cuidando tus posiciones."
            action = "Para volver a recibir compras, cambia el modo a Recomendar."
        else:
            text = f"Solo vigilando: {brake.text}."
            action = (
                "Reactiva las compras cuando hayas leído el resumen semanal."
                if brake.needs_user
                else "Mañana vuelve a proponer compras."
            )
    return {
        "state": state,
        "label": STATE_LABELS[state],
        "tone": _TONES[state],
        "text": text,
        "action": action,
        "problems": problems,
        "incidents": [
            {"component": row.component, "message": row.message, "since": _iso(row.updated_at)}
            for row in incidents
        ],
        "brake": {
            "state": brake.state,
            "reasons": list(brake.reasons),
            "active": list(brake.active),
            "needs_user": brake.needs_user,
        },
        "mode": config.system.mode,
        "ai": ai,
        "_portfolio": portfolio,
        "_history": history,
    }


# --- header ------------------------------------------------------------------------------------


def _latest_macro(session: Session, series_id: str) -> float | None:
    return session.scalar(
        select(models.MacroPoint.value)
        .where(models.MacroPoint.series_id == series_id, models.MacroPoint.value.is_not(None))
        .order_by(models.MacroPoint.obs_date.desc())
        .limit(1)
    )


def _spy_day_pct(session: Session) -> float | None:
    closes = session.scalars(
        select(models.BarDaily.close)
        .where(models.BarDaily.ticker == "SPY")
        .order_by(models.BarDaily.session_date.desc())
        .limit(2)
    ).all()
    return (closes[0] / closes[1] - 1) * 100 if len(closes) == 2 and closes[1] else None


def header(session: Session, config: AppConfig, state: dict, plan: dict | None) -> dict:
    now = clock.now()
    portfolio, history = state["_portfolio"], state["_history"]
    capital = portfolio.capital
    usd_mxn = _latest_macro(session, "DEXMXUS") if config.capital.show_mxn else None
    ideas = (plan or {}).get("ideas", []) if plan and not plan.get("stale") else []
    proposed = [i for i in ideas if i["verdict"] == "COMPRAR" or i["state"] == "BOUGHT"]
    real = [t for t in metrics.closed_trades(session) if not t.is_shadow]
    brier = session.scalar(
        select(models.MetricsDaily.brier)
        .where(models.MetricsDaily.brier.is_not(None))
        .order_by(models.MetricsDaily.day.desc())
        .limit(1)
    )
    profile = profile_for(config.system.aggressiveness)
    return {
        "name": config.system.name,
        "state": state["state"],
        "state_label": state["label"],
        "tone": state["tone"],
        "mode": config.system.mode,
        "aggressiveness": config.system.aggressiveness,
        "profile": {
            "risk_pct": profile.risk_pct,
            "max_positions": profile.max_positions,
            "max_position_pct": profile.max_position_pct,
            "min_score": profile.min_score,
            "min_probability": profile.min_probability,
        },
        "capital_usd": round(capital, 2),
        "cash_usd": round(portfolio.cash, 2),
        "capital_mxn": round(capital * usd_mxn, 2) if usd_mxn else None,
        "usd_mxn": usd_mxn,
        "day_pct": round(portfolio.day_pnl_pct, 2),
        "spy_day_pct": round(spy, 2) if (spy := _spy_day_pct(session)) is not None else None,
        "week_pct": round(portfolio.week_pnl_pct, 2),
        "drawdown_pct": round(risk.drawdown_pct(history, capital), 2),
        "ideas": {
            "followed": sum(1 for i in proposed if i["state"] == "BOUGHT"),
            "proposed": len(proposed),
        },
        "ai": state["ai"],
        "brier": round(brier, 3) if brier is not None else None,
        "cost_per_trade_r": round(sum(t.costs_r for t in real) / len(real), 3) if real else None,
        "clock": {
            "new_york": f"{clock.to_market(now):%H:%M}",
            "user": f"{clock.to_user(now):%H:%M}",
            "user_city": config.system.timezone_user.split("/")[-1].replace("_", " "),
            "date": clock.market_date(now).isoformat(),
            "market": market_calendar.market_status(now),
        },
    }


# --- panels ------------------------------------------------------------------------------------


def current_plan(session: Session) -> dict | None:
    """Today's plan, or the latest one there is (marked as not being today's)."""
    today = clock.market_date(clock.now())
    plan = plan_store.plan_as_dict(session, today)
    if plan is not None:
        return {**plan, "stale": False}
    latest = session.scalar(select(func.max(models.Idea.plan_date)))
    if latest is None:
        return None
    return {**plan_store.plan_as_dict(session, latest), "stale": True}


def alerts_view(session: Session) -> list[dict]:
    order = {"ALTA": 0, "MEDIA": 1, "BAJA": 2}
    alerts = sorted(watching.open_alerts(session), key=lambda a: (order[a.urgency], a.id))
    return [
        {
            "id": alert.id,
            "position_id": alert.position_id,
            "rule": alert.rule,
            "rule_name": watching.RULES[alert.rule].name,
            "kind": watching.RULES[alert.rule].kind,
            "urgency": alert.urgency,
            "message": alert.message,
            "data": alert.data,
            "created_at": _iso(alert.created_at),
            "created_at_label": clock.dual_label(alert.created_at),
            "notified_count": alert.notified_count,
        }
        for alert in alerts
    ]


def positions_view(session: Session, config: AppConfig, *, shadow: bool = False) -> list[dict]:
    today = clock.market_date(clock.now())
    query = select(models.Position).where(
        models.Position.is_shadow.is_(shadow), models.Position.state != "CLOSED"
    )
    rows = [
        p for p in session.scalars(query.order_by(models.Position.id)) if not account.is_voided(p)
    ]
    closes = account.last_prices(session, (p.ticker for p in rows), clock.now())
    ideas = {
        i.id: i
        for i in session.scalars(
            select(models.Idea).where(models.Idea.id.in_([p.idea_id for p in rows]))
        )
    }
    open_alerts: dict[int, list[int]] = {}
    for alert in watching.open_alerts(session):
        open_alerts.setdefault(alert.position_id, []).append(alert.id)
    result = []
    for position in rows:
        tracking = position.tracking or {}
        price = tracking.get("last_price") or closes.get(position.ticker) or position.avg_price
        view = watching.view_of(position, today)
        initial_stop = position.avg_price - position.initial_r
        targets = list(position.targets or [])
        target = next((t for t in targets if t > price), targets[-1] if targets else None)
        span = (target - position.stop) if target is not None else None
        idea = ideas.get(position.idea_id)
        result.append(
            {
                "id": position.id,
                "idea_id": position.idea_id,
                "ticker": position.ticker,
                "name": (idea.details or {}).get("name") if idea is not None else None,
                "setup": idea.setup if idea is not None else None,
                "is_shadow": position.is_shadow,
                "state": position.state,
                "pending_confirmation": position.state == "SELL_ALERT",
                "qty_open": position.qty_open,
                "avg_price": position.avg_price,
                "last_price": price,
                "value_usd": round(position.qty_open * price, 2),
                "pnl_usd": round((price - position.avg_price) * position.qty_open, 2),
                "pnl_pct": round((price / position.avg_price - 1) * 100, 2),
                "r_now": round(view.r_multiple(price), 2),
                "stop": position.stop,
                "initial_stop": round(initial_stop, 2),
                "stop_raised": position.stop > initial_stop + 1e-6,
                "targets": targets,
                "next_target": target,
                # Where the price sits between the stop (0) and the next target (1).
                "progress": round(min(1.0, max(0.0, (price - position.stop) / span)), 3)
                if span and span > 0
                else None,
                "entry_progress": round(
                    min(1.0, max(0.0, (position.avg_price - position.stop) / span)), 3
                )
                if span and span > 0
                else None,
                "days_held": view.days_held,
                "horizon_days": view.horizon_days,
                "opened_at": _iso(position.opened_at),
                "alert_ids": open_alerts.get(position.id, []),
            }
        )
    return result


def closed_view(session: Session, limit: int = 30) -> list[dict]:
    trades = metrics.closed_trades(session)[-limit:]
    return [
        {
            "position_id": t.position_id,
            "idea_id": t.idea_id,
            "ticker": t.ticker,
            "kind": "sombra" if t.is_shadow else "real",
            "setup": t.setup,
            "closed_at": _iso(t.closed_at),
            "closed_at_label": f"{clock.to_market(t.closed_at):%d/%m %H:%M} NY",
            "net_usd": round(t.net_usd, 2),
            "r_multiple": round(t.r_multiple, 2),
            "reason": t.reason,
        }
        for t in reversed(trades)
    ]


def lessons_view(session: Session, config: AppConfig) -> dict:
    rows = list(session.scalars(select(models.Lesson).order_by(models.Lesson.proposed_at.desc())))
    applied: dict[str, int] = {}
    for used in session.scalars(select(models.Idea.lessons_applied)):
        for lesson_id in used or []:
            applied[lesson_id] = applied.get(lesson_id, 0) + 1
    needed, minimum = config.learning.min_cases, config.learning.min_confidence

    def item(row: models.Lesson) -> dict:
        return {
            "id": row.id,
            "kind": row.kind,
            "statement": row.statement,
            "scope": row.scope,
            "status": row.status,
            "cases_total": row.cases_total,
            "cases_forward": row.cases_forward,
            "cases_needed": needed,
            "confidence": round(row.confidence, 3),
            "confidence_needed": minimum,
            "progress": round(min(1.0, row.cases_total / needed), 3),
            "proposed_at": _iso(row.proposed_at),
            "activated_at": _iso(row.activated_at),
            "decisions": applied.get(row.id, 0),
        }

    return {
        "learning": [item(r) for r in rows if r.status == "hypothesis"],
        "learned": [item(r) for r in rows if r.status == "active"],
        "explanation": (
            f"No influye en ninguna decisión. Para empezar a hacerlo necesita {needed} casos "
            f"observados y una confianza de al menos {minimum:.2f}. Ese mínimo lo impone la "
            "base de datos."
        ),
    }


def _latency_by_source(session: Session) -> dict[str, tuple[int | None, datetime]]:
    latest = (
        select(models.RawSnapshot.source, func.max(models.RawSnapshot.id).label("id"))
        .group_by(models.RawSnapshot.source)
        .subquery()
    )
    rows = session.execute(
        select(
            models.RawSnapshot.source, models.RawSnapshot.latency_ms, models.RawSnapshot.fetched_at
        ).join(latest, models.RawSnapshot.id == latest.c.id)
    )
    return {source: (latency, fetched) for source, latency, fetched in rows}


def flow_view(session: Session, config: AppConfig, plan: dict | None, watching_count: int) -> dict:
    """The nodes of "Cómo fluye la información": sources, layers and the path of a
    decision, each with what it last did."""
    health = data_health(session, config.data)
    latency = _latency_by_source(session)
    sources = []
    by_id = {}
    for item in health.sources:
        if item.source == "telegram":
            continue  # an output, not a source of data
        ms, fetched = latency.get(item.source, (None, None))
        node = {
            "id": item.source,
            "label": SOURCE_LABELS.get(item.source, item.source),
            "status": item.status,
            "critical": item.critical,
            "message": item.message,
            "latency_ms": ms,
            "updated_at": _iso(item.updated_at or fetched),
        }
        by_id[item.source] = node
        sources.append(node)
    layers = []
    for layer_id, label, feeds in LAYERS:
        fed = [by_id[s] for s in feeds if s in by_id]
        worst = max(fed, key=lambda n: _SEVERITY.get(n["status"], 0)) if fed else None
        stamps = [n["updated_at"] for n in fed if n["updated_at"]]
        layers.append(
            {
                "id": layer_id,
                "label": label,
                "sources": list(feeds),
                "status": worst["status"] if worst else "unknown",
                "latency_ms": worst["latency_ms"] if worst else None,
                "updated_at": max(stamps) if stamps else None,
            }
        )
    ideas = (plan or {}).get("ideas", [])
    passed = [i for i in ideas if not any(c["result"] == "fail" for c in i.get("controls", []))]
    ai = (plan or {}).get("ai") or {}
    universe = session.scalar(
        select(func.count()).select_from(models.Universe).where(models.Universe.active.is_(True))
    )
    pipeline = [
        (
            "indicadores",
            "Indicadores",
            f"~45 métricas · {(plan or {}).get('screened') or universe or 0} tickers",
        ),
        ("filtro", "Filtro", f"{(plan or {}).get('with_setup') or 0} candidatos"),
        ("controles", "Controles", f"22 comprobaciones · {len(passed)} pasan"),
        ("ia", "Analista IA", ai.get("label") or "sin IA"),
        (
            "riesgo",
            "Motor de riesgo",
            f"{sum(1 for i in ideas if (i.get('shares') or 0) > 0)} con tamaño",
        ),
        ("plan", "Plan del día", f"{len(ideas)} ideas"),
        ("posicion", "Posición", f"vigilando {watching_count}"),
    ]
    return {
        "sources": sources,
        "layers": layers,
        "engine": {"id": "motor", "label": "Motor de datos", "status": health.state},
        "pipeline": [{"id": i, "label": label, "detail": detail} for i, label, detail in pipeline],
        "plan_published_at": (plan or {}).get("published_at"),
    }


def controls_grid(plan: dict | None) -> dict:
    """Candidate x control, with pass, warn or fail and the reason."""
    ideas = (plan or {}).get("ideas", [])
    names = {spec_id: name for spec_id, name in _control_names()}
    return {
        "controls": [{"id": cid, "name": names[cid]} for cid in sorted(names)],
        "rows": [
            {
                "ticker": idea["ticker"],
                "verdict": idea["verdict"],
                "results": {
                    c["id"]: {"result": c["result"], "reason": c["reason"]}
                    for c in idea.get("controls", [])
                },
            }
            for idea in ideas
        ],
    }


def _control_names() -> list[tuple[str, str]]:
    from engine.controls import CONTROLS

    return [(spec.id, spec.name) for spec in CONTROLS]


def dashboard(session: Session, engine: Engine, config: AppConfig) -> dict:
    """Everything the main view shows, in one answer."""
    state = system_state(session, engine, config)
    plan = current_plan(session)
    positions = positions_view(session, config)
    result = {
        "header": header(session, config, state, plan),
        "status": {k: v for k, v in state.items() if not k.startswith("_")},
        "alerts": alerts_view(session),
        "plan": plan,
        "positions": positions,
        "shadows": positions_view(session, config, shadow=True),
        "closed": closed_view(session),
        "lessons": lessons_view(session, config),
        "flow": flow_view(session, config, plan, len(positions)),
        "controls": controls_grid(plan),
        "generated_at": _iso(clock.now()),
    }
    return result


# --- other pages -------------------------------------------------------------------------------


def equity(session: Session) -> dict:
    """The capital curve and the marks of real purchases and sales."""
    marks = []
    real = {p.id: p for p in account.real_positions(session)}
    for fill in session.scalars(
        select(models.Fill)
        .where(models.Fill.position_id.in_(list(real)))
        .order_by(models.Fill.filled_at)
    ):
        position = real[fill.position_id]
        marks.append(
            {
                "day": clock.market_date(fill.filled_at).isoformat(),
                "side": fill.side,
                "ticker": position.ticker,
                "gain": fill.price >= position.avg_price if fill.side == "sell" else None,
            }
        )
    return {"series": metrics.equity_series(session), "marks": marks}


def performance(session: Session) -> dict:
    trades = metrics.closed_trades(session)
    real = [t for t in trades if not t.is_shadow]
    shadow = [t for t in trades if t.is_shadow]
    series = metrics.equity_series(session)
    real_curve = [p["real"] for p in series if p["real"] is not None]
    spy_curve = [p["spy"] for p in series if p["spy"] is not None]
    system_curve = [p["system"] for p in series if p["system"] is not None]

    def total(curve: list[float]) -> float | None:
        return round(curve[-1] - 100, 2) if curve else None

    return {
        "all": metrics.trade_stats(trades).as_dict(),
        "followed": metrics.trade_stats(real).as_dict(),
        "not_followed": metrics.trade_stats(shadow).as_dict(),
        "by_setup": {k: v.as_dict() for k, v in metrics.grouped(trades, lambda t: t.setup).items()},
        "by_regime": {
            k.replace("_", " "): v.as_dict()
            for k, v in metrics.grouped(trades, lambda t: t.regime).items()
        },
        "max_drawdown_pct": round(metrics.max_drawdown_pct(real_curve), 2) if real_curve else None,
        "return_pct": {
            "real": total(real_curve),
            "system": total(system_curve),
            "spy": total(spy_curve),
        },
        "brier": None,
        "reliability": [],
        "note": (
            "Brier y diagrama de confiabilidad llegan con la calibración (Fase 8). "
            "Las sombras se siguen con barras diarias."
        ),
    }


def history(
    session: Session, *, limit: int = 50, offset: int = 0, ticker: str | None = None
) -> dict:
    query = select(models.Idea)
    if ticker:
        query = query.where(models.Idea.ticker == ticker.strip().upper())
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(
        query.order_by(models.Idea.plan_date.desc(), models.Idea.id).limit(limit).offset(offset)
    )
    return {
        "total": total,
        "ideas": [
            {
                "id": idea.id,
                "plan_date": idea.plan_date.isoformat(),
                "ticker": idea.ticker,
                "setup": idea.setup,
                "score": idea.score,
                "verdict": idea.verdict,
                "state": idea.state,
                "no_ai": idea.no_ai,
                "probability": idea.prob_calibrated,
                "ev_r": idea.ev_r,
                "reason": idea.reason,
            }
            for idea in rows
        ],
    }


def idea_audit(session: Session, idea_id: int) -> dict | None:
    """Everything behind one idea: its plan entry, every control, the dossier the AI
    read, what the AI answered and every change of state (principle 8)."""
    idea = session.get(models.Idea, idea_id)
    if idea is None:
        return None
    controls = list(
        session.scalars(
            select(models.ControlResult)
            .where(models.ControlResult.candidate_id == idea.candidate_id)
            .order_by(models.ControlResult.control)
        )
    )
    call = session.get(models.AICall, idea.ai_call_id) if idea.ai_call_id else None
    dossier, answer = None, None
    if call is not None:
        path = Path(call.dossier_path) if call.dossier_path else None
        runs = (settings.repo_root() / "runs").resolve()
        if path is not None and path.is_file() and runs in path.resolve().parents:
            dossier = path.read_text(encoding="utf-8")
        decisions = (call.structured_output or {}).get("decisiones") or []
        answer = next(
            (d for d in decisions if str(d.get("ticker", "")).upper() == idea.ticker), None
        )
    transitions = session.scalars(
        select(models.StateTransition)
        .where(
            models.StateTransition.entity.in_(("idea", plan_store.VERDICT_AUDIT)),
            models.StateTransition.entity_id == str(idea_id),
        )
        .order_by(models.StateTransition.id)
    )
    confirmations = session.scalars(
        select(models.Confirmation)
        .where(models.Confirmation.idea_id == idea_id)
        .order_by(models.Confirmation.id)
    )
    run = session.get(models.Run, idea.run_id)
    return {
        "idea": plan_store.idea_as_dict(idea, controls),
        "plan_date": idea.plan_date.isoformat(),
        "regime": run.regime if run is not None else None,
        "ai_call": {
            "id": call.id,
            "model": call.model,
            "status": call.status,
            "cost_usd": call.cost_usd,
            "duration_ms": call.duration_ms,
            "created_at": _iso(call.created_at),
        }
        if call is not None
        else None,
        "dossier": dossier,
        "ai_answer": answer,
        "transitions": [
            {
                "what": "veredicto" if t.entity == plan_store.VERDICT_AUDIT else "estado",
                "from": t.from_state,
                "to": t.to_state,
                "at": _iso(t.at),
                "at_label": clock.dual_label(t.at),
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
                "at": _iso(c.created_at),
            }
            for c in confirmations
        ],
    }


_LOG_KINDS = ("estado", "alerta", "ia", "corrida", "ajuste", "tarea")


def event_log(session: Session, *, limit: int = 200, kind: str | None = None) -> dict:
    """What the system did, newest first: changes of state, alerts, AI calls, runs,
    settings and daily tasks."""
    events: list[dict[str, Any]] = []

    def add(at: datetime, group: str, text: str, actor: str = "sistema") -> None:
        events.append(
            {
                "at": _iso(at),
                "at_label": f"{clock.to_market(at):%d/%m %H:%M:%S} NY",
                "kind": group,
                "actor": actor,
                "text": text,
            }
        )

    for t in session.scalars(
        select(models.StateTransition).order_by(models.StateTransition.id.desc()).limit(limit)
    ):
        actor = "tú" if t.actor == "user" else "sistema"
        if t.entity == runtime_config.AUDIT_ENTITY:
            add(t.at, "ajuste", t.reason or f"{t.entity_id}: {t.from_state} → {t.to_state}", actor)
        else:
            what = {
                "idea": "Idea",
                "position": "Posición",
                plan_store.VERDICT_AUDIT: "Veredicto de la idea",
            }.get(t.entity, t.entity)
            add(
                t.at,
                "estado",
                f"{what} {t.entity_id}: {t.from_state or 'nueva'} → {t.to_state}"
                + (f" ({t.reason})" if t.reason else ""),
                actor,
            )
    for a in session.scalars(select(models.Alert).order_by(models.Alert.id.desc()).limit(limit)):
        add(a.created_at, "alerta", f"{a.urgency} · {a.message}")
        if a.resolved_at is not None:
            add(a.resolved_at, "alerta", f"Alerta resuelta ({a.resolution}): {a.message}", "tú")
    for c in session.scalars(select(models.AICall).order_by(models.AICall.id.desc()).limit(limit)):
        cost = f" · ${c.cost_usd:.4f}" if c.cost_usd else ""
        add(c.created_at, "ia", f"Consulta a la IA ({c.task}, {c.model}): {c.status}{cost}")
    for r in session.scalars(select(models.Run).order_by(models.Run.id.desc()).limit(limit)):
        stats = r.stats or {}
        add(
            r.finished_at or r.started_at,
            "corrida",
            f"Corrida {r.kind} del {stats.get('plan_date', '')}: régimen "
            f"{(r.regime or '').replace('_', ' ')}",
        )
    for row in session.scalars(
        select(models.SystemStatus).where(models.SystemStatus.component.startswith("job/"))
    ):
        job = json.loads(row.message or "{}")
        outcome = "ok" if job.get("ok") else f"falló ({job.get('attempts')} intentos)"
        add(
            row.updated_at,
            "tarea",
            f"Tarea {row.component.removeprefix('job/')}: {outcome}"
            + (f" · {job.get('detail')}" if job.get("detail") else ""),
        )
    if kind in _LOG_KINDS:
        events = [e for e in events if e["kind"] == kind]
    events.sort(key=lambda e: e["at"], reverse=True)
    return {"kinds": list(_LOG_KINDS), "events": events[:limit]}


def chart(session: Session, ticker: str, *, days: int = 180) -> dict:
    """Daily and five-minute candles of a ticker, with the levels of its latest idea."""
    ticker = ticker.strip().upper()
    daily = session.scalars(
        select(models.BarDaily)
        .where(models.BarDaily.ticker == ticker)
        .order_by(models.BarDaily.session_date.desc())
        .limit(days)
    ).all()
    since = clock.now() - timedelta(days=5)
    intraday = session.scalars(
        select(models.BarIntraday)
        .where(models.BarIntraday.ticker == ticker, models.BarIntraday.ts >= since)
        .order_by(models.BarIntraday.ts)
    ).all()
    idea = session.scalars(
        select(models.Idea)
        .where(models.Idea.ticker == ticker, models.Idea.stop.is_not(None))
        .order_by(models.Idea.plan_date.desc(), models.Idea.id.desc())
        .limit(1)
    ).first()
    name = session.scalar(select(models.Universe.name).where(models.Universe.ticker == ticker))
    return {
        "ticker": ticker,
        "name": name,
        "daily": [
            {
                "time": b.session_date.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in reversed(daily)
        ],
        "intraday": [
            {
                "time": int(b.ts.timestamp()),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in intraday
        ],
        "levels": {
            "plan_date": idea.plan_date.isoformat(),
            "verdict": idea.verdict,
            "entry_min": idea.entry_min,
            "entry_max": idea.entry_max,
            "no_chase_above": idea.no_chase_above,
            "stop": idea.stop,
            "targets": idea.targets,
        }
        if idea is not None
        else None,
    }


def config_view(session: Session, config: AppConfig) -> dict:
    """What the Configuración page shows: what can be changed, what cannot, and the
    state of every API. Never a key."""
    secrets = settings.get_secrets()
    ready = secrets.source_status()
    health = {item.source: item for item in data_health(session, config.data).sources}
    universe = config.universe
    return {
        "editable": [
            {"path": path, "label": label, "value": runtime_config.read_value(config, path)}
            for path, label in runtime_config.EDITABLE.items()
        ],
        "read_only": {
            "Universo": (
                f"{', '.join(universe.exchanges)} · precio mínimo ${universe.min_price:g} · "
                f"liquidez mínima ${universe.min_dollar_volume_20d / 1e6:,.0f} M al día"
            ),
            "Lista SIC": universe.sic_list_path,
            "Horizonte": "1 a 10 días hábiles (swing)",
            "Plan del día": (
                f"se publica {config.plan.publish_time_et:%H:%M} y se revisa "
                f"{config.plan.recheck_time_et:%H:%M}, hora de Nueva York"
            ),
            "Modelos de IA": (
                f"analista {config.ai.analyst_model} · noticias {config.ai.news_model} · "
                f"lecciones {config.ai.lessons_model}"
            ),
            "Zona horaria": (
                f"mercado {config.system.timezone_market} · tú {config.system.timezone_user}"
            ),
        },
        "sources": [
            {
                "source": source,
                "label": SOURCE_LABELS.get(source, source),
                "configured": ready.get(source, False),
                "critical": config.data.sources[source].critical,
                "status": health[source].status if source in health else "unknown",
                "message": health[source].message if source in health else None,
            }
            for source in config.data.sources
        ],
        "ai_login": "API key" if ready.get("anthropic_api") else "sesión de Claude Code",
        "password_set": secrets.dashboard_password is not None,
    }
