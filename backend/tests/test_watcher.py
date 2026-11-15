"""The watcher on synthetic price paths: each exit rule fires at the right moment, alerts
are never duplicated and a restart loses nothing."""

from __future__ import annotations

import pathlib
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core import clock, market_calendar
from db import models
from db.session import session_scope
from engine import confirmations, plan_store, watcher
from engine.confirmations import ConfirmationError
from engine.watcher import Tick, Watcher
from tests.test_screener_plan import AS_OF, CONFIG, DAY, candidate, plan_for

# The position every test starts from: 10 shares of BUY bought at 50.00, with the plan's
# stop at 48.30 (R = 1.70), targets at 53.40 (+2R) and 55.10 (+3R) and an ATR of 1.00.
ENTRY, STOP, TARGET_1, TARGET_2 = 50.0, 48.3, 53.4, 55.1
BOUGHT_AT = AS_OF + timedelta(minutes=45)  # 09:45 New York


@pytest.fixture(autouse=True)
def _clock():
    with clock.simulated(BOUGHT_AT) as simulated:
        yield simulated


@pytest.fixture
def position_id(engine) -> int:
    plan = plan_for([candidate("BUY", 85.0), candidate("TWO", 83.0)] + [
        candidate(f"L{i}", 40.0 - i) for i in range(3)
    ])  # fmt: skip
    with session_scope(engine) as session:
        plan_store.save_plan(session, plan)
        idea = next(i for i in plan_store.todays_ideas(session, DAY) if i.ticker == "BUY")
        return confirmations.confirm_bought(session, idea.id, price=ENTRY, qty=10).id


class Path:
    """Feeds a watcher a price path, one price every ``step`` seconds."""

    def __init__(self, watch: Watcher, simulated, *, step: float = 15.0) -> None:
        self.watch, self.simulated, self.step = watch, simulated, step

    def tick(self, price: float, **fields) -> list[int]:
        self.simulated.advance(timedelta(seconds=self.step))
        return self.watch.on_tick(Tick("BUY", price, clock.now(), **fields))

    def run(self, *prices: float) -> list[tuple[float, list[int]]]:
        """Each price with the alerts it created (only the prices that created any)."""
        fired = []
        for price in prices:
            created = self.tick(price)
            if created:
                fired.append((price, created))
        return fired


@pytest.fixture
def path(engine, position_id, _clock) -> Path:
    return Path(Watcher(engine, CONFIG), _clock)


def alerts(engine, *, open_only: bool = False) -> list[models.Alert]:
    with session_scope(engine) as session:
        query = select(models.Alert).order_by(models.Alert.id)
        if open_only:
            query = query.where(models.Alert.resolved_at.is_(None))
        return list(session.scalars(query))


def position(engine, position_id: int) -> models.Position:
    with session_scope(engine) as session:
        return session.get(models.Position, position_id)


# --- the rules ---------------------------------------------------------------------------------


def test_the_eleven_rules_in_priority_order():
    assert [(spec.number, spec.name, spec.urgency) for spec in watcher.RULES.values()] == [
        (1, "Stop", "ALTA"), (2, "Noticia grave", "ALTA"), (3, "Objetivo 1", "MEDIA"),
        (4, "Stop móvil", "BAJA"), (5, "Objetivo 2", "MEDIA"), (6, "Resultados", "MEDIA"),
        (7, "Tesis invalidada", "MEDIA"), (8, "Régimen", "MEDIA"), (9, "Tiempo", "BAJA"),
        (10, "Horizonte", "BAJA"), (11, "Rotación", "BAJA"),
    ]  # fmt: skip


def test_the_watcher_does_not_depend_on_the_ai():
    """The stops must work with the AI down: the watcher does not even import it."""
    imported = set(sys.modules["engine.watcher"].__dict__)
    assert not {name for name in imported if name.startswith("ai_")}
    source = pathlib.Path(sys.modules["engine.watcher"].__file__).read_text(encoding="utf-8")
    assert "ai_gateway" not in source and "ai_analyst" not in source and "ai_client" not in source


def test_stop_fires_at_the_first_price_at_or_below_it(engine, position_id, path):
    fired = path.run(50.2, 49.6, 48.9, 48.31, 48.30, 48.1, 47.9)

    assert [price for price, _ in fired] == [48.30]  # not at 48.31, and only once after
    (alert,) = alerts(engine)
    assert (alert.rule, alert.urgency, alert.position_id) == ("stop", "ALTA", position_id)
    assert alert.message == "BUY · Stop: el precio tocó $48.30 y tu stop está en $48.30"
    data = alert.data
    assert (data["action"], data["sell_qty"]) == ("Vender todo ya", 10)
    assert (data["price_source"], data["rule_number"]) == ("alpaca:iex", 1)
    # The alert shows the latest price and the result so far, in $, % and R.
    assert data["price"] == 47.9
    assert (data["pnl_usd"], data["pnl_pct"], data["r_now"]) == (-21.0, -4.2, -1.24)
    assert alert.created_at == BOUGHT_AT + timedelta(seconds=5 * 15)
    assert position(engine, position_id).state == "SELL_ALERT"


def test_an_open_below_the_stop_is_the_stop_rule_too(engine, position_id, path):
    created = path.tick(47.5, is_open=True)

    assert len(created) == 1
    (alert,) = alerts(engine)
    assert alert.rule == "stop"
    assert alert.data["explanation"] == "abrió en $47.50, debajo de tu stop de $48.30"


def test_first_target_asks_to_sell_half_and_protect_the_rest(engine, position_id, path):
    fired = path.run(50.5, 51.8, 53.39, 53.40, 53.6)

    assert [price for price, _ in fired][-1] == 53.40
    target = next(a for a in alerts(engine) if a.rule == "objetivo_1")
    assert target.urgency == "MEDIA"
    assert target.data["action"] == (
        "Vender 5 acciones (50%) y subir el stop a $50.00, tu precio de entrada"
    )
    assert target.data["explanation"] == "llegó al objetivo 1 de $53.40 (+2.0R)"
    assert (target.data["sell_qty"], target.data["new_stop"]) == (5, 50.0)
    assert position(engine, position_id).state == "SELL_ALERT"


def test_second_target_asks_to_sell_the_rest(engine, position_id, path):
    path.run(53.5)
    with session_scope(engine) as session:
        first = watcher.open_alert(session, position_id, "objetivo_1")
        confirmations.confirm_sold(session, position_id, price=53.4, qty=5, alert_id=first.id)

    fired = path.run(54.2, 55.09, 55.10)

    assert [price for price, _ in fired] == [55.10]
    target = next(a for a in alerts(engine) if a.rule == "objetivo_2")
    assert (target.data["action"], target.data["sell_qty"]) == ("Vender el resto", 5)


def test_trailing_stop_starts_at_one_and_a_half_r_and_only_moves_up(engine, position_id, path):
    # +1.5R is 52.55. From there the stop trails 2.5 ATR under the highest price.
    fired = path.run(51.0, 52.0, 52.54, 52.55, 52.2, 53.0, 52.6)

    assert [price for price, _ in fired] == [52.55]
    (alert,) = alerts(engine)
    assert (alert.rule, alert.urgency) == ("stop_movil", "BAJA")
    # One alert, whose suggestion followed the highest price (53.00 - 2.50) and never fell.
    assert alert.data["new_stop"] == 50.5
    assert position(engine, position_id).tracking["highest"] == 53.0
    # Asking to raise the stop does not put the position in "sell alert".
    assert position(engine, position_id).state == "OPEN"
    # The stop of the position only changes when the user says they moved it.
    assert position(engine, position_id).stop == STOP


def test_time_stop_fires_at_the_close_review_of_the_fifth_day(engine, position_id, _clock):
    watch = Watcher(engine, CONFIG)
    days = [market_calendar.add_trading_days(DAY, n) for n in range(1, 6)]
    fired = {}
    for held, day in enumerate(days, start=1):
        _clock.set(clock.market_time(day, clock.now().time().replace(hour=15, minute=30)))
        watch.on_tick(Tick("BUY", 50.4, clock.now()))  # never reaches +0.5R (50.85)
        fired[held] = watch.close_review({"BUY": 50.4})

    assert [held for held, created in fired.items() if created] == [5]
    (alert,) = alerts(engine)
    assert (alert.rule, alert.urgency) == ("tiempo", "BAJA")
    assert alert.data["action"] == "Vender al cierre"
    assert alert.data["explanation"] == (
        "lleva 5 días hábiles y nunca llegó a +0.5R (máximo +0.2R)"
    )


def test_no_time_stop_for_a_trade_that_reached_half_an_r(engine, position_id, _clock):
    watch = Watcher(engine, CONFIG)
    watch.on_tick(Tick("BUY", 50.9, clock.now()))  # +0.53R on the first day
    _clock.set(clock.market_time(market_calendar.add_trading_days(DAY, 5), AS_OF.time()))

    assert watch.close_review({"BUY": 50.3}) == []
    # ...but the horizon of the idea (10 days when the AI gave none) still applies.
    _clock.set(clock.market_time(market_calendar.add_trading_days(DAY, 10), AS_OF.time()))
    (alert_id,) = watch.close_review({"BUY": 50.3})
    assert alerts(engine)[0].rule == "horizonte" and alerts(engine)[0].id == alert_id


def test_earnings_and_a_numeric_invalidation_are_checked_before_the_close(
    engine, position_id, _clock
):
    with session_scope(engine) as session:
        idea_id = session.get(models.Position, position_id).idea_id
        session.get(models.Idea, idea_id).invalidation = "Cierre diario debajo de 49.20"
    watch = Watcher(engine, CONFIG)

    created = watch.close_review({"BUY": 49.0}, reports_soon=["BUY", "OTHER"])

    assert len(created) == 2
    by_rule = {a.rule: a for a in alerts(engine)}
    assert by_rule["resultados"].data["action"] == "Vender antes del cierre de hoy"
    assert by_rule["tesis_invalidada"].data["explanation"] == (
        "se cumplió la invalidación del plan: el precio ($49.00) está debajo de $49.20"
    )
    assert watcher.invalidation_level("si pierde el soporte por malas noticias") is None
    assert watcher.invalidation_level("cierre bajo 44,44 (promedio de 21 días)") == 44.44


def test_high_risk_regime_asks_to_tighten_what_is_below_one_r(engine, position_id, path):
    assert path.tick(50.6) == []

    created = path.watch.on_tick(
        Tick("BUY", 50.6, clock.now() + timedelta(seconds=20)), regime="RIESGO_ALTO"
    )

    assert len(created) == 1
    (alert,) = alerts(engine)
    assert (alert.rule, alert.urgency, alert.data["kind"]) == ("regimen", "MEDIA", "either")
    assert alert.data["action"] == "Sube tu stop a $49.60 (1 ATR) o vende la mitad"
    assert (alert.data["new_stop"], alert.data["sell_qty"]) == (49.6, 5)


def test_rotation_proposals_become_low_urgency_suggestions(engine, position_id):
    watch = Watcher(engine, CONFIG)
    proposal = {"sell": "BUY", "buy": "NEW", "edge_r": 0.9, "text": "vende BUY para comprar NEW"}

    created = watch.suggest_rotations([proposal, {**proposal, "sell": "NOT_HELD"}])

    assert len(created) == 1
    (alert,) = alerts(engine)
    assert (alert.rule, alert.urgency) == ("rotacion", "BAJA")
    assert position(engine, position_id).state == "OPEN"  # a suggestion, not a sell alert


# --- no duplicates -----------------------------------------------------------------------------


def test_one_alert_per_position_and_rule_however_long_the_price_stays_there(
    engine, position_id, path
):
    fired = path.run(*([48.2, 48.0, 47.8, 48.25, 47.5] * 20))

    assert len(fired) == 1
    assert len(alerts(engine)) == 1
    assert alerts(engine)[0].data["price"] == 47.5  # the open alert shows the latest price


def test_the_database_itself_refuses_a_second_open_alert_of_the_same_rule(
    engine, position_id, path
):
    path.run(48.0)

    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            models.Alert(position_id=position_id, rule="stop", urgency="ALTA", message="dup",
                         created_at=clock.now())
        )  # fmt: skip
    # A resolved alert does not block a new one of the same rule.
    with session_scope(engine) as session:
        confirmations.ignore_alert(session, alerts(engine)[0].id, reason="otro")
        session.add(
            models.Alert(position_id=position_id, rule="stop", urgency="ALTA", message="new",
                         created_at=clock.now())
        )  # fmt: skip
    assert len(alerts(engine)) == 2


def test_rules_are_evaluated_at_most_every_ten_seconds_without_missing_a_dip(
    engine, position_id, _clock
):
    watch = Watcher(engine, CONFIG)
    start = clock.now()

    def at(seconds: float, price: float) -> list[int]:
        return watch.on_tick(Tick("BUY", price, start + timedelta(seconds=seconds)))

    assert at(0, 50.0) == []
    # A dip through the stop and back, all inside the ten-second window: not evaluated yet.
    assert at(3, 48.0) == []
    assert at(6, 49.4) == []
    # The next evaluation still knows the price was at 48.00.
    created = at(10, 49.5)
    assert len(created) == 1
    assert alerts(engine)[0].data["explanation"] == (
        "el precio tocó $48.00 y tu stop está en $48.30"
    )


# --- a restart loses nothing -------------------------------------------------------------------


def test_after_a_restart_the_watcher_resumes_the_same_positions_and_state(
    engine, position_id, _clock
):
    before = Path(Watcher(engine, CONFIG), _clock)
    before.run(51.0, 52.6, 53.0)  # trailing stop suggested at 50.50
    watched = before.watch.watched_tickers()
    sent = before.watch.due_notifications()
    for alert_id in sent:
        before.watch.mark_notified(alert_id)

    after = Path(Watcher(engine, CONFIG), _clock)  # a new process: nothing in memory

    assert after.watch.watched_tickers() == watched == ["BUY"]
    assert after.watch.due_notifications() == []  # nothing is sent twice
    assert after.run(52.4, 52.8) == []  # the same alert is still open: no duplicate
    assert position(engine, position_id).tracking["highest"] == 53.0  # not forgotten
    # It carries on from where the other one was: the next rule fires at its level.
    fired = after.run(53.2, 53.4)
    assert [price for price, _ in fired] == [53.4]
    assert [a.rule for a in alerts(engine)] == ["stop_movil", "objetivo_1"]
    assert alerts(engine)[0].data["new_stop"] == pytest.approx(50.9)  # 53.40 - 2.50


def test_a_restart_in_the_middle_of_a_stop_alert_does_not_repeat_or_lose_it(
    engine, position_id, _clock
):
    first = Path(Watcher(engine, CONFIG), _clock)
    first.run(48.2)
    first.watch.mark_notified(alerts(engine)[0].id)

    second = Path(Watcher(engine, CONFIG), _clock)

    assert second.run(48.1, 47.9) == []
    assert len(alerts(engine)) == 1 and position(engine, position_id).state == "SELL_ALERT"
    # The repeats of the high-urgency alert continue on schedule after the restart.
    assert second.watch.due_notifications() == []
    _clock.advance(timedelta(minutes=5))
    assert second.watch.due_notifications() == [alerts(engine)[0].id]


# --- notifications -----------------------------------------------------------------------------


def test_high_urgency_alerts_repeat_every_five_minutes_three_times(
    engine, position_id, path, _clock
):
    path.run(48.0)
    watch = path.watch
    (alert_id,) = watch.due_notifications()
    sent = 0
    for _ in range(12):  # an hour, checking every five minutes
        for due in watch.due_notifications():
            watch.mark_notified(due)
            sent += 1
        _clock.advance(timedelta(minutes=5))

    assert sent == 4  # the first one and three repeats
    assert alerts(engine)[0].notified_count == 4
    # The position is still open and waiting: the system never assumes a sale.
    assert position(engine, position_id).state == "SELL_ALERT"
    assert position(engine, position_id).qty_open == 10


def test_other_alerts_are_sent_once(engine, position_id, path, _clock):
    path.run(53.5)
    watch = path.watch
    first = watch.due_notifications()
    for alert_id in first:
        watch.mark_notified(alert_id)
    _clock.advance(timedelta(minutes=30))

    assert len(first) == 2 and watch.due_notifications() == []  # objetivo 1 and stop móvil


# --- answering an alert ------------------------------------------------------------------------


def test_sold_answers_the_alert_and_records_the_real_result(engine, position_id, path):
    path.run(48.2)
    (alert,) = alerts(engine)

    with session_scope(engine) as session:
        confirmations.confirm_sold(
            session, position_id, price=48.25, qty=10, commission=1.2, alert_id=alert.id
        )

    (alert,) = alerts(engine)
    assert alert.resolution == "vendido: 10 a $48.25" and alert.resolved_at is not None
    after = position(engine, position_id)
    assert (after.state, after.qty_open) == ("CLOSED", 0)
    with session_scope(engine) as session:
        sale = session.scalars(
            select(models.Confirmation).where(models.Confirmation.kind == "sold")
        ).one()
        assert (sale.alert_id, sale.data["reason"]) == (alert.id, "stop")
        # R with the price actually paid, and dollars net of commissions.
        assert sale.data["r_multiple"] == pytest.approx(-1.029, abs=1e-3)
        assert sale.data["position_net_usd"] == pytest.approx(-18.70)
        assert sale.data["position_r"] == pytest.approx(-1.1)
        # A sale at the stop starts the cooldown of that ticker.
        from engine import account

        assert account.load_portfolio(session, CONFIG).stopped_out == {"BUY": DAY}


def test_selling_half_at_the_first_target_then_asks_to_protect_the_rest(engine, position_id, path):
    path.run(53.4)
    with session_scope(engine) as session:
        target = watcher.open_alert(session, position_id, "objetivo_1")
        confirmations.confirm_sold(session, position_id, price=53.45, qty=5, alert_id=target.id)

    after = position(engine, position_id)
    assert (after.state, after.qty_open, after.tracking["target_1_done"]) == (
        "PARTIALLY_CLOSED", 5, True,
    )  # fmt: skip
    trailing = next(a for a in alerts(engine, open_only=True) if a.rule == "stop_movil")
    assert trailing.data["new_stop"] >= ENTRY
    # "Ya subí el stop" moves it to the suggested level and answers that alert.
    with session_scope(engine) as session:
        confirmations.confirm_stop_raised(session, position_id, alert_id=trailing.id)
    after = position(engine, position_id)
    assert after.stop == trailing.data["new_stop"] and alerts(engine, open_only=True) == []
    # The first target does not ask again, and the new stop is the one that is watched.
    assert path.run(53.4, 53.1) == []
    fired = path.run(52.0, after.stop)
    assert [price for price, _ in fired] == [after.stop]
    assert alerts(engine, open_only=True)[0].rule == "stop"


def test_the_stop_only_moves_up(engine, position_id):
    with (
        pytest.raises(ConfirmationError, match="el stop solo sube"),
        session_scope(engine) as session,
    ):
        confirmations.confirm_stop_raised(session, position_id, new_stop=47.0)
    with (
        pytest.raises(ConfirmationError, match="el stop solo sube"),
        session_scope(engine) as session,
    ):
        confirmations.confirm_stop_raised(session, position_id, new_stop=STOP)
    with session_scope(engine) as session:
        confirmations.confirm_stop_raised(session, position_id, new_stop=49.0, note="a mano")

    assert position(engine, position_id).stop == 49.0
    with session_scope(engine) as session:
        raised = session.scalars(
            select(models.Confirmation).where(models.Confirmation.kind == "stop_raised")
        ).one()
        assert raised.data == {"ticker": "BUY", "from": STOP, "to": 49.0}


def test_ignoring_an_alert_keeps_the_position_open_and_asks_again_tomorrow(
    engine, position_id, path, _clock
):
    path.run(48.2)
    with session_scope(engine) as session:
        confirmations.ignore_alert(session, alerts(engine)[0].id, reason="fue un pico")

    (alert,) = alerts(engine)
    assert alert.resolution == "ignorada: fue un pico"
    after = position(engine, position_id)
    assert (after.state, after.qty_open) == ("OPEN", 10)  # the system never assumes a sale
    # The same rule does not insist today...
    assert path.run(48.0, 47.5) == []
    # ...but a stop still breached tomorrow is raised again.
    _clock.set(clock.market_time(market_calendar.add_trading_days(DAY, 1), AS_OF.time()))
    assert len(path.run(47.9)) == 1
    assert [a.resolved_at is None for a in alerts(engine)] == [False, True]


def test_an_answered_alert_cannot_be_answered_again(engine, position_id, path):
    path.run(48.2)
    alert_id = alerts(engine)[0].id
    with session_scope(engine) as session:
        confirmations.ignore_alert(session, alert_id)

    with (
        pytest.raises(ConfirmationError, match="ya fue resuelta"),
        session_scope(engine) as session,
    ):
        confirmations.ignore_alert(session, alert_id)
    with pytest.raises(confirmations.NotFound), session_scope(engine) as session:
        confirmations.ignore_alert(session, 999_999)


# --- endpoints ---------------------------------------------------------------------------------


@pytest.fixture
def client(position_id, db_url, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", db_url)
    with TestClient(create_app()) as test_client:
        yield test_client


def test_alerts_and_their_answers_through_the_api(engine, position_id, path, client):
    assert client.get("/api/alerts").json() == {"alerts": []}
    path.run(52.8)  # the trailing stop asks for 50.30
    path.run(48.2)  # and then the price falls through the stop

    listed = client.get("/api/alerts").json()["alerts"]
    assert [(a["rule"], a["urgency"]) for a in listed] == [("stop", "ALTA"), ("stop_movil", "BAJA")]
    assert listed[0]["rule_name"] == "Stop" and listed[0]["data"]["action"] == "Vender todo ya"
    (held,) = client.get("/api/positions").json()["positions"]
    assert (held["state"], held["pending_confirmation"]) == ("SELL_ALERT", True)

    stop, trailing = listed
    ignored = client.post(f"/api/alerts/{stop['id']}/ignore", json={"reason": "fue un pico"})
    assert ignored.json()["alert"]["resolution"] == "ignorada: fue un pico"
    raised = client.post(
        f"/api/positions/{position_id}/stop-raised", json={"alert_id": trailing["id"]}
    )
    assert raised.json()["position"]["stop"] == 50.3
    assert client.get("/api/alerts").json() == {"alerts": []}
    assert len(client.get("/api/alerts", params={"include_resolved": True}).json()["alerts"]) == 2
    (held,) = client.get("/api/positions").json()["positions"]
    assert (held["state"], held["pending_confirmation"]) == ("OPEN", False)

    lower = client.post(f"/api/positions/{position_id}/stop-raised", json={"new_stop": 49.0})
    assert lower.status_code == 422 and "el stop solo sube" in lower.json()["detail"]
    assert client.post("/api/alerts/999/ignore").status_code == 404


def test_sold_through_the_api_answers_its_alert(engine, position_id, path, client):
    path.run(48.2)
    (alert,) = client.get("/api/alerts").json()["alerts"]

    sold = client.post(
        f"/api/positions/{position_id}/sold",
        json={"price": 48.25, "qty": 10, "alert_id": alert["id"]},
    )

    assert sold.json()["position"]["state"] == "CLOSED"
    assert client.get("/api/alerts").json() == {"alerts": []}


def test_a_failed_daily_task_shows_in_the_system_status(engine, position_id, client):
    with session_scope(engine) as session:
        session.add(
            models.SystemStatus(
                component="job/premarket", status="error", updated_at=clock.now(),
                message='{"day": "2026-11-10", "ok": false, "detail": "sin datos", "attempts": 3}',
            )
        )  # fmt: skip

    status = client.get("/api/status").json()

    assert status["state"] == "PRECAUCION"
    assert "Falló la tarea premarket: sin datos" in status["problems"]
