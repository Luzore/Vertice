"""What the dashboard reads and the few things it changes: the session, the main
view, the other pages, the settings and the "explain this idea" button.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import auth
from core import clock
from db import models
from db.session import get_engine, session_scope
from engine import overview, plan_store, runtime_config
from engine.ai_analyst import build_gateway, runs_dir
from engine.ai_client import TASK_EXPLAIN
from engine.ai_gateway import AIGateway

public = APIRouter()
router = APIRouter(dependencies=[Depends(auth.require_session)])


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=500)


class Changes(BaseModel):
    changes: dict[str, Any]


# --- session -----------------------------------------------------------------------------------


@public.get("/session")
def session_state(request: Request) -> dict:
    """Whether a password is set and whether this browser is logged in."""
    return {
        "configured": auth.configured(),
        "authenticated": auth.configured() and auth.valid_token(request.cookies.get(auth.COOKIE)),
    }


@public.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    if not auth.configured():
        raise HTTPException(
            503, "falta DASHBOARD_PASSWORD en .env: sin contraseña nadie puede entrar"
        )
    if auth.locked_out():
        raise HTTPException(429, "demasiados intentos fallidos: espera unos minutos")
    if not auth.check_password(body.password):
        raise HTTPException(401, "contraseña incorrecta")
    auth.reset_failures()
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        auth.COOKIE,
        auth.new_token(),
        max_age=auth.SESSION_SECONDS,
        httponly=True,
        samesite="strict",
        secure=secure,
    )
    return {"authenticated": True}


@public.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE)
    return {"authenticated": False}


# --- main view and pages -----------------------------------------------------------------------


@router.get("/dashboard")
def dashboard() -> dict:
    engine = get_engine()
    with session_scope(engine) as session:
        config = runtime_config.current(session=session)
        return overview.dashboard(session, engine, config)


@router.get("/equity")
def equity() -> dict:
    with session_scope(get_engine()) as session:
        return overview.equity(session)


@router.get("/performance")
def performance() -> dict:
    with session_scope(get_engine()) as session:
        return overview.performance(session)


@router.get("/history")
def history(limit: int = 50, offset: int = 0, ticker: str | None = None) -> dict:
    with session_scope(get_engine()) as session:
        return overview.history(
            session, limit=min(max(limit, 1), 200), offset=max(offset, 0), ticker=ticker
        )


@router.get("/history/{idea_id}")
def idea_audit(idea_id: int) -> dict:
    with session_scope(get_engine()) as session:
        audit = overview.idea_audit(session, idea_id)
    if audit is None:
        raise HTTPException(404, f"no existe la idea {idea_id}")
    return audit


@router.get("/log")
def event_log(limit: int = 200, kind: str | None = None) -> dict:
    with session_scope(get_engine()) as session:
        return overview.event_log(session, limit=min(max(limit, 1), 500), kind=kind)


@router.get("/chart/{ticker}")
def chart(ticker: str) -> dict:
    with session_scope(get_engine()) as session:
        data = overview.chart(session, ticker)
    if not data["daily"]:
        raise HTTPException(404, f"no hay barras de {ticker.upper()}")
    return data


# --- settings ----------------------------------------------------------------------------------


@router.get("/config")
def config_view() -> dict:
    with session_scope(get_engine()) as session:
        return overview.config_view(session, runtime_config.current(session=session))


@router.put("/config")
def config_update(body: Changes) -> dict:
    """Change settings from the dashboard: mode, aggressiveness, cash, costs... Every
    change is validated like the configuration file and audited."""
    with session_scope(get_engine()) as session:
        try:
            config = runtime_config.update(session, body.changes)
        except runtime_config.SettingError as problem:
            raise HTTPException(422, str(problem)) from problem
        return overview.config_view(session, config)


@router.post("/system/pause")
def pause() -> dict:
    """The stop button: no new purchases until the user switches the mode back. The
    watcher keeps looking after the open positions."""
    with session_scope(get_engine()) as session:
        runtime_config.update(session, {"system.mode": "paused"})
    return {"mode": "paused"}


# --- "Explícame" -------------------------------------------------------------------------------


def explain_gateway() -> AIGateway:
    """The AI behind the explain button (replaced by the simulated one in tests)."""
    engine = get_engine()
    return build_gateway(
        runtime_config.current(engine), engine=engine, dossier_dir=runs_dir() / "dossiers"
    )


@router.post("/ideas/{idea_id}/explain")
def explain(idea_id: int) -> dict:
    """A more didactic explanation of one idea, written by the AI from the idea's own
    data. It counts against the daily budget; the same idea is answered from the
    cache for half an hour."""
    with session_scope(get_engine()) as session:
        idea = session.get(models.Idea, idea_id)
        if idea is None:
            raise HTTPException(404, f"no existe la idea {idea_id}")
        data = plan_store.idea_as_dict(idea)
    block = {
        "ticker": data["ticker"],
        "empresa": data["name"],
        "veredicto": data["verdict"],
        "motivo_si_no_es_compra": data["reason"],
        "setup": data["setup"],
        "puntaje": data["score"],
        "desglose_del_puntaje": data["breakdown"],
        "probabilidad": data["probability"],
        "entrada": data["entry"],
        "stop": data["stop"],
        "objetivos": data["targets"],
        "objetivos_en_r": data["targets_r"],
        "expectativa_en_r": data["ev_r"],
        "por_que": data["notes"],
        "tesis": data["thesis"],
        "razones": data["reasons"],
        "riesgos": data["risks"],
        "invalidacion": data["invalidation"],
        "avisos": data["warnings"],
    }
    body = json.dumps(block, ensure_ascii=False, indent=1)
    dossier = f"# Idea por explicar · {data['ticker']}\n\n```json\n{body}\n```\n"
    result = explain_gateway().call(TASK_EXPLAIN, dossier)
    if not result.ok:
        return {"available": False, "reason": result.note, "at": clock.now().isoformat()}
    return {"available": True, **result.output}
