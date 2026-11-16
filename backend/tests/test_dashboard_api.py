"""The API behind the dashboard: password, main view, pages, settings and live changes."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app import auth
from app import main as app_main
from app.api import dashboard as dashboard_api
from app.main import create_app
from core import clock, settings
from db import migrate, models
from db import session as db_session
from db.session import session_scope
from engine import confirmations, plan_store, runtime_config
from engine.ai_analyst import build_gateway
from engine.ai_client import SimulatedAI
from engine.ai_gateway import MemoryLedger
from engine.watcher import Tick, Watcher
from tests.conftest import SHIPPED_CONFIG, TEST_PASSWORD, TEST_SECRETS, logged_in_client
from tests.test_screener_plan import candidate, plan_for

CONFIG = settings.load_config(SHIPPED_CONFIG)
MIXED = [candidate("BUY", 85.0), candidate("TWO", 83.0), candidate("NEAR", 65.0),
         candidate("LOW", 40.0), candidate("CAP", 88.0)]  # fmt: skip


@pytest.fixture
def seeded(engine) -> dict[str, int]:
    """Today's plan saved (the real clock: the dashboard shows "today")."""
    from dataclasses import replace

    today = clock.market_date(clock.now())
    plan = replace(plan_for(MIXED, caps={"CAP": 4e8}), plan_date=today, as_of=clock.now())
    with session_scope(engine) as session:
        plan_store.save_plan(session, plan)
    with session_scope(engine) as session:
        return {idea.ticker: idea.id for idea in plan_store.todays_ideas(session, today)}


@pytest.fixture
def client(seeded, db_url, monkeypatch):
    with logged_in_client(monkeypatch, db_url, keys=True) as test_client:
        yield test_client


def anonymous(db_url, monkeypatch, *, password: str | None = TEST_PASSWORD) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", db_url)
    if password is None:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    settings.reset_caches()
    db_session.get_engine.cache_clear()
    auth.reset_failures()
    migrate.upgrade(db_url)
    return TestClient(create_app())


# --- password ----------------------------------------------------------------------------------

PROTECTED = [
    ("get", "/api/dashboard"), ("get", "/api/plan"), ("get", "/api/positions"),
    ("get", "/api/alerts"), ("get", "/api/account"), ("get", "/api/status"),
    ("get", "/api/status/sources"), ("get", "/api/config"), ("get", "/api/equity"),
    ("get", "/api/performance"), ("get", "/api/history"), ("get", "/api/log"),
    ("get", "/api/chart/SPY"), ("post", "/api/ideas/1/bought"), ("post", "/api/ideas/1/declined"),
    ("post", "/api/positions/1/sold"), ("post", "/api/system/pause"), ("put", "/api/config"),
    ("post", "/api/ideas/1/explain"), ("post", "/api/alerts/1/ignore"),
]  # fmt: skip


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_nothing_of_the_dashboard_is_public(db_url, monkeypatch, method, path):
    with anonymous(db_url, monkeypatch) as client:
        response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.json() == {"detail": "inicia sesión para continuar"}


def test_without_a_password_nobody_gets_in(db_url, monkeypatch):
    with anonymous(db_url, monkeypatch, password=None) as client:
        assert client.get("/api/session").json() == {"configured": False, "authenticated": False}
        assert client.get("/api/dashboard").status_code == 503
        login = client.post("/api/login", json={"password": "cualquiera"})
        assert login.status_code == 503 and "DASHBOARD_PASSWORD" in login.json()["detail"]
        # The health check stays open: it is what Docker asks, and it says nothing private.
        assert client.get("/api/health").status_code == 200


def test_login_opens_a_session_and_logout_closes_it(db_url, monkeypatch):
    with anonymous(db_url, monkeypatch) as client:
        assert client.get("/api/session").json() == {"configured": True, "authenticated": False}
        wrong = client.post("/api/login", json={"password": "no-es-esta"})
        assert wrong.status_code == 401 and wrong.json() == {"detail": "contraseña incorrecta"}

        right = client.post("/api/login", json={"password": TEST_PASSWORD})

        assert right.status_code == 200
        cookie = right.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=2592000" in cookie
        assert client.get("/api/session").json()["authenticated"] is True
        assert client.get("/api/dashboard").status_code == 200
        client.post("/api/logout")
        assert client.get("/api/dashboard").status_code == 401


def test_a_forged_or_expired_session_is_refused(db_url, monkeypatch):
    with anonymous(db_url, monkeypatch) as client:
        good = auth.new_token()
        expires, _, signature = good.partition(".")
        assert auth.valid_token(good)
        assert not auth.valid_token(f"{int(expires) + 1000}.{signature}")  # altered expiry
        assert not auth.valid_token(f"{expires}.{'0' * 64}")
        past = int(time.time()) - 10
        assert not auth.valid_token(f"{past}.{auth._sign(past)}")  # noqa: SLF001
        assert not auth.valid_token("") and not auth.valid_token(None)
        client.cookies.set(auth.COOKIE, f"{expires}.{'0' * 64}")
        assert client.get("/api/dashboard").status_code == 401


def test_five_wrong_passwords_lock_the_login(db_url, monkeypatch):
    with anonymous(db_url, monkeypatch) as client:
        for _ in range(5):
            assert client.post("/api/login", json={"password": "x"}).status_code == 401

        locked = client.post("/api/login", json={"password": TEST_PASSWORD})

        assert locked.status_code == 429
        assert locked.json() == {"detail": "demasiados intentos fallidos: espera unos minutos"}


# --- the main view -----------------------------------------------------------------------------


def test_dashboard_has_every_panel_of_the_main_view(client, seeded):
    data = client.get("/api/dashboard").json()

    assert set(data) >= {
        "header", "status", "alerts", "plan", "positions", "closed", "lessons", "flow",
        "controls",
    }  # fmt: skip
    header = data["header"]
    assert (header["name"], header["state"], header["state_label"]) == (
        "VERTICE", "NORMAL", "NORMAL",
    )  # fmt: skip
    assert (header["mode"], header["aggressiveness"]) == ("recommend", 5)
    assert (header["capital_usd"], header["cash_usd"]) == (5000.0, 5000.0)
    assert header["ideas"] == {"followed": 0, "proposed": 2}
    assert header["ai"]["limit"] == 40 and header["ai"]["used"] == 0
    assert header["clock"]["user_city"] == "Monterrey"
    assert header["clock"]["market"] in ("open", "premarket", "closed")
    assert data["status"]["text"].startswith("Operando con normalidad")
    assert [i["verdict"] for i in data["plan"]["ideas"]][:2] == ["COMPRAR", "COMPRAR"]
    assert data["plan"]["stale"] is False
    # Candidate x control: 22 controls for each of the five ideas.
    grid = data["controls"]
    assert len(grid["controls"]) == 22 and grid["controls"][0] == {
        "id": "C01", "name": "Datos frescos",
    }  # fmt: skip
    assert all(len(row["results"]) == 22 for row in grid["rows"])
    cap = next(row for row in grid["rows"] if row["ticker"] == "CAP")
    assert cap["results"]["C05"]["result"] == "fail"
    # The flow: sources, the seven layers and the path of a decision.
    flow = data["flow"]
    assert [layer["label"] for layer in flow["layers"]] == [
        "Mercado", "Volatilidad", "Sentimiento", "Noticias", "Macro", "Calendario", "Eventos",
    ]  # fmt: skip
    assert [node["label"] for node in flow["pipeline"]] == [
        "Indicadores", "Filtro", "Controles", "Analista IA", "Motor de riesgo", "Plan del día",
        "Posición",
    ]  # fmt: skip
    assert flow["pipeline"][2]["detail"] == "22 comprobaciones · 4 pasan"
    assert {source["id"] for source in flow["sources"]} >= {"alpaca", "finnhub", "fred"}
    assert data["lessons"]["learning"] == [] and "20 casos" in data["lessons"]["explanation"]


def test_positions_alerts_and_closed_trades_show_up_as_they_happen(client, seeded, engine):
    bought = client.post(f"/api/ideas/{seeded['BUY']}/bought", json={"price": 50.0, "qty": 10})
    position_id = bought.json()["position"]["id"]
    watch = Watcher(engine, CONFIG)
    watch.on_tick(Tick("BUY", 52.0, clock.now()))

    data = client.get("/api/dashboard").json()

    (position,) = data["positions"]
    assert (position["ticker"], position["last_price"], position["r_now"]) == ("BUY", 52.0, 1.18)
    assert (position["pnl_usd"], position["pnl_pct"]) == (20.0, 4.0)
    assert (position["stop"], position["next_target"], position["stop_raised"]) == (
        48.3, 53.4, False,
    )  # fmt: skip
    # 52.00 is 73% of the way from the stop (48.30) to the next target (53.40).
    assert position["progress"] == pytest.approx(0.725, abs=1e-3)
    assert data["header"]["cash_usd"] == 4500.0 and data["header"]["capital_usd"] == 5020.0
    assert data["header"]["ideas"] == {"followed": 1, "proposed": 2}
    assert data["alerts"] == []

    watch.on_tick(Tick("BUY", 48.0, clock.now() + timedelta(seconds=30)))
    data = client.get("/api/dashboard").json()
    (alert,) = data["alerts"]
    assert (alert["rule_name"], alert["urgency"], alert["kind"]) == ("Stop", "ALTA", "sell")
    assert data["positions"][0]["pending_confirmation"] is True

    client.post(
        f"/api/positions/{position_id}/sold",
        json={"price": 48.2, "qty": 10, "alert_id": alert["id"]},
    )
    data = client.get("/api/dashboard").json()
    assert data["positions"] == [] and data["alerts"] == []
    (closed,) = data["closed"]
    assert (closed["ticker"], closed["kind"], closed["reason"]) == ("BUY", "real", "saltó el stop")
    assert (closed["net_usd"], closed["r_multiple"]) == (-18.0, -1.06)


def test_without_a_plan_today_the_latest_one_is_shown_as_old(db_url, monkeypatch, engine):
    from dataclasses import replace

    old = replace(plan_for(MIXED), plan_date=clock.market_date(clock.now()) - timedelta(days=5))
    with session_scope(engine) as session:
        plan_store.save_plan(session, old)

    with logged_in_client(monkeypatch, db_url, keys=True) as client:
        data = client.get("/api/dashboard").json()

    assert data["plan"]["stale"] is True
    assert data["header"]["ideas"] == {"followed": 0, "proposed": 0}


# --- the state in the header -------------------------------------------------------------------


def test_missing_data_keys_show_as_data_down(db_url, monkeypatch, seeded):
    with logged_in_client(monkeypatch, db_url) as client:  # no data keys at all
        status = client.get("/api/dashboard").json()["status"]

    assert (status["state"], status["label"], status["tone"]) == (
        "DATOS_CAIDOS", "DATOS CAÍDOS", "bad",
    )  # fmt: skip
    assert status["text"].startswith("Datos caídos: Fuente crítica alpaca sin configurar")
    assert "Configuración" in status["action"]


def test_mode_pause_and_ai_quota_change_the_state(client):
    def state() -> tuple[str, str]:
        status = client.get("/api/dashboard").json()["status"]
        return status["state"], status["text"]

    client.put("/api/config", json={"changes": {"system.mode": "watch_only"}})
    assert state() == (
        "SOLO_VIGILAR",
        "Solo vigilando: no propone compras nuevas; sigue cuidando tus posiciones.",
    )
    client.put("/api/config", json={"changes": {"system.mode": "recommend"}})
    client.put("/api/config", json={"changes": {"ai.daily_call_budget": 0}})
    assert state()[0] == "SIN_CUOTA_IA"
    assert "el vigilante sigue funcionando" in state()[1]

    assert client.post("/api/system/pause").json() == {"mode": "paused"}
    assert state()[0] == "PAUSA"
    assert client.get("/api/dashboard").json()["header"]["mode"] == "paused"


# --- settings ----------------------------------------------------------------------------------


def test_settings_changed_in_the_dashboard_are_validated_audited_and_shared(client, engine):
    before = client.get("/api/config").json()
    values = {item["path"]: item["value"] for item in before["editable"]}
    assert values["system.aggressiveness"] == 5 and values["capital.cash_usd"] == 5000
    assert before["password_set"] is True

    changed = client.put(
        "/api/config",
        json={"changes": {"system.aggressiveness": 8, "capital.cash_usd": 6200}},
    )

    assert changed.status_code == 200
    header = client.get("/api/dashboard").json()["header"]
    assert (header["aggressiveness"], header["cash_usd"]) == (8, 6200.0)
    assert header["profile"]["max_positions"] == 6  # between the rows of 7 and 10
    # The worker reads the same settings.
    assert runtime_config.current(engine).system.aggressiveness == 8
    with session_scope(engine) as session:
        audit = session.scalars(
            select(models.StateTransition)
            .where(models.StateTransition.entity == "setting")
            .order_by(models.StateTransition.id)
        ).all()
        assert [(a.entity_id, a.from_state, a.to_state, a.actor) for a in audit] == [
            ("system.aggressiveness", "5", "8", "user"),
            ("capital.cash_usd", "5000.0", "6200.0", "user"),
        ]


@pytest.mark.parametrize(
    "changes",
    [
        {"system.aggressiveness": 11},
        {"system.mode": "trade"},
        {"capital.cash_usd": -5},
        {"controls.daily_loss_stop_pct": 50},  # not something the dashboard may change
        {"learning.min_cases": 5},
    ],
)
def test_an_invalid_setting_changes_nothing(client, engine, changes):
    response = client.put("/api/config", json={"changes": {**changes, "costs.slippage_pct": 0.1}})

    assert response.status_code == 422
    # All or nothing: the valid change that came with it was not applied either.
    assert runtime_config.current(engine) == settings.get_config()


def test_no_secret_ever_reaches_the_browser(client):
    text = "".join(
        client.get(path).text
        for path in ("/api/dashboard", "/api/config", "/api/status", "/api/log", "/api/session")
    )

    for value in [*TEST_SECRETS.values(), TEST_PASSWORD]:
        assert value not in text
    sources = {s["source"]: s for s in client.get("/api/config").json()["sources"]}
    assert sources["alpaca"]["configured"] is True and "key" not in json.dumps(sources).lower()


# --- other pages -------------------------------------------------------------------------------


def test_history_and_the_audit_of_one_idea(client, seeded):
    listed = client.get("/api/history").json()
    assert listed["total"] == 5 and {i["ticker"] for i in listed["ideas"]} >= {"BUY", "CAP"}
    assert client.get("/api/history", params={"ticker": "buy"}).json()["total"] == 1
    client.post(f"/api/ideas/{seeded['BUY']}/declined", json={"reason": "sin efectivo"})

    audit = client.get(f"/api/history/{seeded['BUY']}").json()

    assert audit["idea"]["ticker"] == "BUY" and len(audit["idea"]["controls"]) == 22
    assert [(t["what"], t["to"], t["actor"]) for t in audit["transitions"]] == [
        ("estado", "PROPOSED", "system"),
        ("estado", "DECLINED", "user"),
    ]
    assert audit["confirmations"][0]["kind"] == "declined"
    assert audit["ai_call"] is None and audit["dossier"] is None  # a plan made without the AI
    assert client.get("/api/history/999999").status_code == 404


def test_performance_equity_log_and_chart(client, seeded, engine):
    bought = client.post(f"/api/ideas/{seeded['BUY']}/bought", json={"price": 50.0, "qty": 10})
    position_id = bought.json()["position"]["id"]
    client.post(f"/api/positions/{position_id}/sold", json={"price": 53.4, "qty": 10})
    today = clock.market_date(clock.now())
    with session_scope(engine) as session:
        for offset, (real, system, spy) in enumerate([(5000, 5000, 600), (5034, 5050, 606)]):
            session.add(
                models.MetricsDaily(
                    day=today - timedelta(days=1 - offset), equity_real=real,
                    equity_system=system, spy_close=spy,
                )
            )  # fmt: skip
        session.add_all(
            models.BarDaily(ticker="BUY", session_date=today - timedelta(days=n), open=50,
                            high=51, low=49, close=50.5, volume=1e6, source="t",
                            updated_at=clock.now())
            for n in range(3)
        )  # fmt: skip

    performance = client.get("/api/performance").json()
    assert performance["followed"]["count"] == 1
    assert performance["followed"]["win_rate_pct"] == 100.0
    assert performance["followed"]["expectancy_r"] == pytest.approx(2.0)
    assert performance["not_followed"]["count"] == 0
    assert performance["by_setup"]["RUPTURA"]["count"] == 1
    assert performance["return_pct"] == {"real": 0.68, "system": 1.0, "spy": 1.0}

    equity = client.get("/api/equity").json()
    assert [p["real"] for p in equity["series"]] == [100.0, 100.68]
    assert [(m["side"], m["ticker"], m["gain"]) for m in equity["marks"]] == [
        ("buy", "BUY", None),
        ("sell", "BUY", True),
    ]

    log = client.get("/api/log").json()
    assert "estado" in log["kinds"]
    assert any("Posición" in e["text"] and "CLOSED" in e["text"] for e in log["events"])
    assert {
        e["kind"] for e in client.get("/api/log", params={"kind": "corrida"}).json()["events"]
    } == {"corrida"}

    chart = client.get("/api/chart/buy").json()
    assert len(chart["daily"]) == 3 and chart["levels"]["stop"] == 48.3
    assert chart["levels"]["targets"] == [53.4, 55.1]
    assert client.get("/api/chart/NADA").status_code == 404


# --- "Explícame" -------------------------------------------------------------------------------


def test_explain_asks_the_ai_about_one_idea_within_the_budget(client, seeded, monkeypatch):
    ai, ledger = SimulatedAI(), MemoryLedger()
    monkeypatch.setattr(
        dashboard_api, "explain_gateway", lambda: build_gateway(CONFIG, client=ai, ledger=ledger)
    )

    answer = client.post(f"/api/ideas/{seeded['BUY']}/explain").json()

    assert answer["available"] is True
    assert answer["explicacion"].startswith("IA simulada: BUY quedó en COMPRAR")
    assert answer["conceptos"][0]["termino"] == "R"
    (call,) = ai.calls
    assert call.task == "explain" and '"ticker": "BUY"' in call.dossier
    # Never an amount of money or a number of shares in what the AI is sent.
    assert "acciones" not in call.dossier and "amount" not in call.dossier
    client.post(f"/api/ideas/{seeded['BUY']}/explain")
    assert len(ai.calls) == 1  # the same idea is answered from the cache


def test_explain_says_why_when_the_ai_cannot_answer(client, seeded, monkeypatch):
    no_quota = CONFIG.model_copy(
        update={"ai": CONFIG.ai.model_copy(update={"daily_call_budget": 0})}
    )
    monkeypatch.setattr(
        dashboard_api,
        "explain_gateway",
        lambda: build_gateway(no_quota, client=SimulatedAI(), ledger=MemoryLedger()),
    )

    answer = client.post(f"/api/ideas/{seeded['BUY']}/explain").json()

    assert answer["available"] is False and answer["reason"].startswith("SIN CUOTA IA")
    assert client.post("/api/ideas/999999/explain").status_code == 404


# --- live changes ------------------------------------------------------------------------------


def test_the_live_channel_needs_a_session(db_url, monkeypatch):
    with anonymous(db_url, monkeypatch) as client:
        with pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect("/ws"):
            pass
        assert closed.value.code == 4401


def test_a_change_in_the_database_reaches_the_browser_in_under_two_seconds(client, seeded, engine):
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "hello"
        time.sleep(0.7)  # let the feed take its first reading

        started = time.monotonic()
        # Another connection (the worker, in real life) writes to the database.
        with session_scope(engine) as session:
            confirmations.confirm_declined(session, seeded["TWO"], reason="otro")
        message = socket.receive_json()
        elapsed = time.monotonic() - started

    assert message["type"] == "changed"
    assert elapsed < 2.0
    ideas = client.get("/api/dashboard").json()["plan"]["ideas"]
    assert next(i for i in ideas if i["ticker"] == "TWO")["state"] == "DECLINED"


# --- the built dashboard -----------------------------------------------------------------------


def test_the_built_dashboard_is_served_with_its_assets_cached(db_url, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>VERTICE</title><div id=root></div>")
    (dist / "assets" / "app-abc123.js").write_text("console.log('ok')")
    monkeypatch.setattr(app_main, "dashboard_dir", lambda: dist)

    with anonymous(db_url, monkeypatch) as client:
        page = client.get("/")
        deep = client.get("/rendimiento")
        asset = client.get("/assets/app-abc123.js")

        assert "VERTICE" in page.text and page.headers["cache-control"] == "no-cache"
        assert deep.text == page.text  # every route of the app gets the same page
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert client.get("/api/no-existe").status_code == 404


def test_without_a_build_the_page_says_how_to_make_one(db_url, monkeypatch, tmp_path):
    monkeypatch.setattr(app_main, "dashboard_dir", lambda: tmp_path / "missing")

    with anonymous(db_url, monkeypatch) as client:
        page = client.get("/")

    assert "make frontend" in page.text
