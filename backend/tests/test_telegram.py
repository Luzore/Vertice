"""Telegram: the messages, the buttons and what each answer does."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx2 as httpx
import pytest
from sqlalchemy import select

from core import clock
from db import models
from db.session import session_scope
from engine import confirmations, plan_store, states
from engine import watcher as watching
from engine.watcher import Tick, Watcher
from notify import messages as msg
from notify.messages import OutMessage
from notify.telegram import AWAITING_KEY, TelegramBot, TelegramClient
from tests.conftest import TEST_SECRETS
from tests.test_screener_plan import AS_OF, CONFIG, DAY, candidate, plan_for

pytestmark = pytest.mark.anyio

CHAT = TEST_SECRETS["telegram_chat_id"]
MIXED = [candidate("BUY", 85.0), candidate("TWO", 83.0), candidate("NEAR", 65.0),
         candidate("LOW", 40.0), candidate("CAP", 88.0)]  # fmt: skip


class FakeTelegram:
    """Telegram in memory: keeps what was sent and hands out queued updates."""

    def __init__(self) -> None:
        self.sent: list[OutMessage] = []
        self.queue: list[dict] = []
        self.answered: list[str] = []
        self.failing = False

    async def send(self, message: OutMessage) -> int | None:
        if self.failing:
            return None
        self.sent.append(message)
        return len(self.sent)

    async def updates(self, offset: int | None) -> list[dict]:
        return [u for u in self.queue if offset is None or u["update_id"] >= offset]

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append(callback_id)

    @property
    def last(self) -> str:
        return self.sent[-1].text


def press(data: str, *, chat: str = CHAT, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "message": {"chat": {"id": int(chat)}},
        },
    }


def say(text: str, *, chat: str = CHAT, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": int(chat)}, "text": text}}


@pytest.fixture(autouse=True)
def _clock():
    with clock.simulated(AS_OF) as simulated:
        yield simulated


@pytest.fixture
def ideas(engine) -> dict[str, int]:
    plan = plan_for(MIXED, caps={"CAP": 4e8})
    with session_scope(engine) as session:
        plan_store.save_plan(session, plan)
    with session_scope(engine) as session:
        return {idea.ticker: idea.id for idea in plan_store.todays_ideas(session, DAY)}


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def bot(engine, ideas, telegram) -> TelegramBot:
    return TelegramBot(engine, CONFIG, telegram, CHAT)


def buttons(message: OutMessage) -> list[tuple[str, str]]:
    return [(button.text, button.data) for row in message.buttons for button in row]


def real_position(engine) -> models.Position:
    with session_scope(engine) as session:
        return session.scalars(
            select(models.Position).where(models.Position.is_shadow.is_(False))
        ).one()


# --- the plan ----------------------------------------------------------------------------------


async def test_the_plan_is_sent_with_one_message_per_purchase_and_its_buttons(bot, telegram, ideas):
    sent = await bot.publish_plan(DAY)

    assert sent == len(telegram.sent) == 4
    header, buy, two, rest = telegram.sent
    assert header.text.startswith("VERTICE · Plan del día · mar 10 nov 2026 · Régimen ALCISTA")
    assert "2 para COMPRAR · 1 para VIGILAR · 2 NO HOY." in header.text
    assert header.buttons == ()
    assert buy.text.splitlines() == [
        "COMPRAR  BUY  (RUPTURA · puntaje 85 · sin IA)",
        "BUY Corp.",
        "Entrada: límite 49.75–50.25 (no perseguir sobre 50.75)",
        "Stop: 48.30 (−1R)   Objetivos: 53.40 (+2.0R) / 55.10 (+3.0R)",
        "Tamaño: 9 acciones ≈ $452 (9% del capital) · riesgo $18",
        "Expectativa: +0.30R después de costos",
        "Por qué: rompe máximo de 20 días",
    ]
    assert buttons(buy) == [
        ("La compré", f"buy:{ideas['BUY']}"),
        ("No la compré", f"no:{ideas['BUY']}"),
    ]
    assert buttons(two)[0][1] == f"buy:{ideas['TWO']}"
    assert "VIGILAR  NEAR (RUPTURA · puntaje 65): puntaje 65, cerca del mínimo de 70" in rest.text
    assert "Se activa si: cierre de vela de 5 min arriba de $50.25" in rest.text
    assert "NO HOY  CAP (RUPTURA · puntaje 88): C05 Tamaño de empresa" in rest.text
    assert "Cómo entrar: orden límite dentro de la zona, nunca a mercado" in rest.text


async def test_a_day_without_purchases_says_so(engine, telegram):
    weak = [candidate(f"W{i}", 60.0 - i) for i in range(5)]
    with session_scope(engine) as session:
        plan_store.save_plan(session, plan_for(weak))

    await TelegramBot(engine, CONFIG, telegram, CHAT).publish_plan(DAY)

    assert "Hoy no hay ninguna idea en COMPRAR" in telegram.sent[0].text
    assert all(message.buttons == () for message in telegram.sent)


async def test_without_a_plan_it_says_there_is_none(engine, telegram):
    await TelegramBot(engine, CONFIG, telegram, CHAT).publish_plan(DAY)

    assert telegram.last == "Todavía no hay Plan del día para el 2026-11-10."


# --- "La compré" / "No la compré" --------------------------------------------------------------


async def test_bought_asks_for_price_and_quantity_and_records_the_real_purchase(
    engine, bot, telegram, ideas
):
    await bot.handle(press(f"buy:{ideas['BUY']}"))

    assert telegram.answered == ["cb1"]  # the button stops spinning
    assert telegram.last.startswith(
        "BUY: ¿a qué precio promedio y cuántas acciones compraste? El plan sugería 9 acciones."
    )
    with session_scope(engine) as session:
        assert session.scalars(select(models.Position)).all() == []  # nothing yet

    await bot.handle(say("50,10 9 1.31"))

    position = real_position(engine)
    assert (position.avg_price, position.qty_open, position.state) == (50.10, 9, "OPEN")
    assert telegram.last == (
        "Registrado: compraste 9 BUY a $50.10. Tienes 9 a $50.10 en promedio. Stop en $48.30: "
        "arriesgas $16.20 (R = $1.80 por acción). Te aviso cuándo vender."
    )
    with session_scope(engine) as session:
        fill = session.scalars(select(models.Fill)).one()
        assert (fill.commission, fill.origin) == (1.31, "user")
        assert session.get(models.SystemStatus, AWAITING_KEY).message is None


async def test_an_answer_that_is_not_two_numbers_is_asked_again(engine, bot, telegram, ideas):
    await bot.handle(press(f"buy:{ideas['BUY']}"))
    await bot.handle(say("sí, la compré"))

    assert telegram.last.startswith("Necesito dos números: precio y cantidad")
    await bot.handle(say("47.00 9"))  # below the stop: refused, still waiting
    assert telegram.last.startswith(
        "No se pudo: el precio de compra ($47.00) debe ser mayor al stop"
    )
    await bot.handle(say("/cancelar"))
    assert telegram.last == "Cancelado: no registré nada."
    with session_scope(engine) as session:
        assert session.scalars(select(models.Position)).all() == []
        assert session.get(models.Idea, ideas["BUY"]).state == "PROPOSED"


async def test_declined_asks_why_and_records_it(engine, bot, telegram, ideas):
    await bot.handle(press(f"no:{ideas['TWO']}"))

    assert telegram.last == "¿Por qué no la compraste?"
    assert buttons(telegram.sent[-1]) == [
        ("Sin efectivo", f"why:{ideas['TWO']}:0"),
        ("No me convenció", f"why:{ideas['TWO']}:1"),
        ("Llegué tarde", f"why:{ideas['TWO']}:2"),
        ("Otro", f"why:{ideas['TWO']}:3"),
    ]

    await bot.handle(press(f"why:{ideas['TWO']}:2"))

    assert telegram.last.startswith("Anotado: no compraste TWO (llegué tarde).")
    with session_scope(engine) as session:
        idea = session.get(models.Idea, ideas["TWO"])
        confirmation = session.scalars(select(models.Confirmation)).one()
        assert idea.state == "DECLINED" and confirmation.data["reason"] == "llegué tarde"


async def test_only_the_configured_chat_is_obeyed(engine, bot, telegram, ideas):
    await bot.handle(press(f"buy:{ideas['BUY']}", chat="999"))
    await bot.handle(say("50.10 9", chat="999"))
    await bot.handle(press(f"why:{ideas['BUY']}:0", chat="999"))

    assert telegram.sent == [] and telegram.answered == []
    with session_scope(engine) as session:
        assert session.get(models.Idea, ideas["BUY"]).state == "PROPOSED"
        assert session.scalars(select(models.Position)).all() == []


async def test_an_invalid_transition_from_telegram_changes_nothing_and_warns(
    engine, bot, telegram, ideas
):
    with session_scope(engine) as session:
        idea = session.get(models.Idea, ideas["BUY"])
        states.transition(session, "idea", idea, "EXPIRED", actor="system", reason="cierre")
        states.transition(session, "idea", idea, "NOT_FILLABLE", actor="system", reason="x")

    await bot.handle(press(f"why:{ideas['BUY']}:0"))

    assert telegram.last.startswith("PRECAUCIÓN: transición inválida: la idea")
    with session_scope(engine) as session:
        assert session.get(models.Idea, ideas["BUY"]).state == "NOT_FILLABLE"
        assert len(states.open_incidents(session)) == 1


# --- alerts ------------------------------------------------------------------------------------


@pytest.fixture
def held(engine, ideas, _clock) -> int:
    """10 shares of BUY bought at 50.00 (stop 48.30, targets 53.40 and 55.10)."""
    _clock.advance(timedelta(minutes=45))
    with session_scope(engine) as session:
        return confirmations.confirm_bought(session, ideas["BUY"], price=50.0, qty=10).id


def tick(watch: Watcher, price: float, _clock, seconds: float = 15) -> list[int]:
    _clock.advance(timedelta(seconds=seconds))
    return watch.on_tick(Tick("BUY", price, clock.now()))


async def test_a_sell_alert_says_what_fired_the_price_the_result_and_what_to_do(
    engine, bot, telegram, held, _clock
):
    watch = Watcher(engine, CONFIG)
    (alert_id,) = tick(watch, 48.10, _clock)

    assert await bot.send_alerts(watch) == 1

    assert telegram.last.splitlines() == [
        "🔴 ALTA · BUY · Stop",
        "Vender todo ya",
        "Motivo: el precio tocó $48.10 y tu stop está en $48.30.",
        "Precio: $48.10 (alpaca:iex, 09:45 NY · 08:45 Monterrey)",
        "Resultado: −$19.00 · −3.8% · −1.12R",
        "Si no haces nada: la pérdida deja de estar limitada: el plan era salir aquí.",
        "La posición sigue ABIERTA hasta que me digas qué hiciste.",
    ]
    assert buttons(telegram.sent[-1]) == [
        ("Vendí", f"sold:{alert_id}"),
        ("Ignorar", f"ign:{alert_id}"),
    ]


async def test_alerts_are_sent_once_and_high_urgency_ones_repeat(
    engine, bot, telegram, held, _clock
):
    watch = Watcher(engine, CONFIG)
    tick(watch, 48.10, _clock)

    counts = []
    for _ in range(8):
        counts.append(await bot.send_alerts(watch))
        _clock.advance(timedelta(minutes=5))

    assert counts == [1, 1, 1, 1, 0, 0, 0, 0]  # the alert and three repeats, then silence
    assert [m.text.splitlines()[0] for m in telegram.sent] == [
        "🔴 ALTA · BUY · Stop",
        "Recordatorio 1 de 3 · 🔴 ALTA · BUY · Stop",
        "Recordatorio 2 de 3 · 🔴 ALTA · BUY · Stop",
        "Recordatorio 3 de 3 · 🔴 ALTA · BUY · Stop",
    ]


async def test_an_alert_telegram_did_not_accept_is_sent_again(engine, bot, telegram, held, _clock):
    watch = Watcher(engine, CONFIG)
    tick(watch, 53.5, _clock)  # objetivo 1 (and the trailing stop)
    telegram.failing = True

    assert await bot.send_alerts(watch) == 0

    telegram.failing = False
    assert await bot.send_alerts(watch) == 2
    assert await bot.send_alerts(watch) == 0


async def test_sold_from_an_alert_records_the_sale_and_the_real_result(
    engine, bot, telegram, held, _clock
):
    watch = Watcher(engine, CONFIG)
    (alert_id,) = tick(watch, 48.10, _clock)

    await bot.handle(press(f"sold:{alert_id}"))
    assert telegram.last.startswith(
        "BUY: ¿a qué precio y cuántas acciones vendiste? Tienes 10; la alerta sugería vender 10."
    )
    await bot.handle(say("48.25 10 1.2"))

    assert telegram.last == (
        "Registrado: vendiste 10 BUY a $48.25. Posición cerrada. Resultado neto de comisiones: "
        "−$18.70 (−1.10R)."
    )
    position = real_position(engine)
    assert (position.state, position.qty_open) == ("CLOSED", 0)
    with session_scope(engine) as session:
        assert watching.open_alerts(session) == []


async def test_selling_part_says_what_is_left(engine, bot, telegram, held, _clock):
    watch = Watcher(engine, CONFIG)
    created = tick(watch, 53.5, _clock)
    with session_scope(engine) as session:
        target = watching.open_alert(session, held, "objetivo_1")
    assert target.id in created

    await bot.handle(press(f"sold:{target.id}"))
    assert "la alerta sugería vender 5" in telegram.last
    await bot.handle(say("53.45 5"))

    assert telegram.last == (
        "Registrado: vendiste 5 BUY a $53.45. Te quedan 5 acciones con stop en $48.30."
    )
    assert real_position(engine).state == "PARTIALLY_CLOSED"


async def test_stop_raised_and_ignore_buttons(engine, bot, telegram, held, _clock):
    watch = Watcher(engine, CONFIG)
    tick(watch, 52.8, _clock)  # +1.6R: the trailing stop asks for 50.30
    with session_scope(engine) as session:
        trailing = watching.open_alert(session, held, "stop_movil")
    await bot.send_alerts(watch)
    assert buttons(telegram.sent[-1]) == [
        ("Ya subí el stop", f"stop:{trailing.id}"),
        ("Ignorar", f"ign:{trailing.id}"),
    ]

    await bot.handle(press(f"stop:{trailing.id}"))

    assert telegram.last == "Listo: el stop de BUY quedó en $50.30. Ya vigilo ese nivel."
    assert real_position(engine).stop == 50.3

    (stop_alert,) = tick(watch, 50.2, _clock)
    await bot.handle(press(f"ign:{stop_alert}"))
    assert telegram.last == "¿Por qué ignoras esta alerta?"
    await bot.handle(press(f"ignw:{stop_alert}:0"))

    assert telegram.last.startswith(
        "Anotado: ignoraste la alerta de BUY · Stop (prefiero esperar)."
    )
    position = real_position(engine)
    assert (position.state, position.qty_open) == ("OPEN", 10)  # never assumed sold


async def test_answering_an_alert_twice_is_explained(engine, bot, telegram, held, _clock):
    watch = Watcher(engine, CONFIG)
    (alert_id,) = tick(watch, 48.10, _clock)
    await bot.handle(press(f"ignw:{alert_id}:3"))

    await bot.handle(press(f"sold:{alert_id}"))

    assert telegram.last == "No se pudo: esa alerta ya fue resuelta (ignorada: otro)."


async def test_reminders_of_what_waits_for_an_answer(engine, bot, telegram, ideas, held, _clock):
    watch = Watcher(engine, CONFIG)
    tick(watch, 48.10, _clock)

    assert await bot.remind_pending(DAY) == 2

    purchase, alert = telegram.sent
    assert purchase.text.startswith(
        "Pendiente: ¿compraste TWO? El plan de hoy la propuso (9 acciones"
    )
    assert buttons(purchase)[0] == ("La compré", f"buy:{ideas['TWO']}")
    assert alert.text.startswith("Pendiente de tu confirmación: BUY · Stop. Vender todo ya.")


async def test_commands(engine, bot, telegram, held):
    await bot.handle(say("/estado"))
    assert telegram.last.splitlines()[:3] == [
        "Efectivo: $4,500.00 · Capital: $5,000.00",
        "Posiciones abiertas: 1",
        "· BUY: 10 a $50.00, último $50.00 (+0.00R), stop $48.30",
    ]

    await bot.handle(say("/plan"))
    assert any(m.text.startswith("VERTICE · Plan del día") for m in telegram.sent)
    await bot.handle(say("hola"))
    assert telegram.last.startswith("Comandos:")


async def test_updates_are_processed_once_even_after_a_restart(engine, bot, telegram, ideas):
    telegram.queue = [
        press(f"no:{ideas['TWO']}", update_id=70),
        press(f"why:{ideas['TWO']}:0", update_id=71),
    ]

    assert await bot.poll_once() == 2
    sent = len(telegram.sent)
    # A new bot (after a restart) starts from the saved offset: nothing is replayed.
    restarted = TelegramBot(engine, CONFIG, telegram, CHAT)
    assert await restarted.poll_once() == 0
    assert len(telegram.sent) == sent
    with session_scope(engine) as session:
        assert len(session.scalars(select(models.Confirmation)).all()) == 1


# --- the client --------------------------------------------------------------------------------


async def test_client_sends_text_and_buttons_to_the_configured_chat(make_runtime):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = TelegramClient(make_runtime(handler))
    message = OutMessage("hola", ((msg.Button("La compré", "buy:1"),),))

    assert await client.send(message) == 77

    (request,) = seen
    assert request.method == "GET"
    assert request.url.path.endswith("/sendMessage")
    params = dict(request.url.params)
    assert (params["chat_id"], params["text"]) == (CHAT, "hola")
    assert json.loads(params["reply_markup"]) == {
        "inline_keyboard": [[{"text": "La compré", "callback_data": "buy:1"}]]
    }


async def test_client_failures_return_none_and_never_log_the_token(make_runtime, capsys):
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert await TelegramClient(make_runtime(refused)).send(OutMessage("x")) is None
    assert await TelegramClient(make_runtime(broken)).send(OutMessage("x")) is None
    assert await TelegramClient(make_runtime(broken)).updates(None) == []
    output = capsys.readouterr()
    assert TEST_SECRETS["telegram_bot_token"] not in output.out + output.err
