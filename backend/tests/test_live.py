"""Live prices, triggers of the watched ideas, AI review of positions and the service
that runs the day."""

from __future__ import annotations

import copy
import json
from datetime import date, time, timedelta

import httpx2 as httpx
import pytest
from sqlalchemy import select

from collectors.alpaca import AlpacaCollector
from collectors.price_feed import AlpacaStream, FallbackFeed, PollingFeed
from core import clock, settings
from db import models
from db.session import session_scope
from engine import confirmations, plan_store, watch_triggers
from engine import position_review as review
from engine import watcher as watching
from engine.ai_client import SimulatedAI
from engine.service import Service
from engine.watch_triggers import Candle, CandleBuilder
from engine.watcher import Tick
from notify.telegram import TelegramBot
from tests.conftest import SHIPPED_CONFIG, TEST_SECRETS
from tests.test_ai_analyst import gateway
from tests.test_screener_plan import AS_OF, DAY, candidate, plan_for
from tests.test_telegram import FakeTelegram, press, say

pytestmark = pytest.mark.anyio

CONFIG = settings.load_config(SHIPPED_CONFIG)
CHAT = TEST_SECRETS["telegram_chat_id"]
OPEN = clock.market_time(DAY, time(9, 30))


@pytest.fixture(autouse=True)
def _clock():
    with clock.simulated(AS_OF) as simulated:
        yield simulated


def at(hour: int, minute: int, second: int = 0, day: date = DAY):
    return clock.market_time(day, time(hour, minute, second))


# --- polling feed ------------------------------------------------------------------------------


def snapshots(**trades: tuple[float, str]) -> dict:
    return {
        symbol: {"latestTrade": {"p": price, "t": stamp, "s": 100}, "dailyBar": {"o": price}}
        for symbol, (price, stamp) in trades.items()
    }


async def test_polling_feed_returns_a_tick_only_for_a_new_trade(make_runtime, _clock):
    _clock.set(at(10, 0, 5))
    answers = [
        snapshots(BUY=(50.1, "2026-11-10T15:00:01Z"), TWO=(30.0, "2026-11-10T15:00:02Z")),
        snapshots(BUY=(50.1, "2026-11-10T15:00:01Z"), TWO=(30.2, "2026-11-10T15:00:09Z")),
    ]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answers[min(len(seen), len(answers)) - 1])

    feed = PollingFeed(AlpacaCollector(make_runtime(handler)))

    first = await feed.poll(["TWO", "BUY"])
    _clock.advance(timedelta(seconds=10))
    second = await feed.poll(["TWO", "BUY"])

    assert [(t.ticker, t.price, t.source, t.is_open) for t in first] == [
        ("BUY", 50.1, "alpaca:iex", False),
        ("TWO", 30.0, "alpaca:iex", False),
    ]
    assert [(t.ticker, t.price) for t in second] == [("TWO", 30.2)]  # BUY did not trade again
    assert seen[0].url.path == "/v2/stocks/snapshots"
    assert dict(seen[0].url.params) == {"symbols": "BUY,TWO", "feed": "iex"}
    assert feed.problem is None


async def test_polling_feed_marks_the_first_price_after_the_open(make_runtime, _clock):
    _clock.set(at(9, 30, 20))
    payload = snapshots(BUY=(47.5, "2026-11-10T14:30:12Z"))
    feed = PollingFeed(AlpacaCollector(make_runtime(lambda r: httpx.Response(200, json=payload))))

    (tick,) = await feed.poll(["BUY"])

    assert (tick.price, tick.is_open) == (47.5, True)


async def test_polling_feed_gives_nothing_when_the_source_fails_or_is_stale(make_runtime, _clock):
    _clock.set(at(11, 0))
    broken = PollingFeed(AlpacaCollector(make_runtime(lambda r: httpx.Response(500, text="x"))))
    old = snapshots(BUY=(50.0, "2026-11-10T15:40:00Z"))  # twenty minutes old, market open
    stale = PollingFeed(AlpacaCollector(make_runtime(lambda r: httpx.Response(200, json=old))))

    assert await broken.poll(["BUY"]) == [] and broken.problem
    assert await stale.poll(["BUY"]) == []
    assert "el precio más reciente tiene" in stale.problem
    assert await broken.poll([]) == []


# --- the stream --------------------------------------------------------------------------------


class FakeSocket:
    """A WebSocket that plays a script of frames and records what it is sent."""

    def __init__(self, frames: list[list[dict]]) -> None:
        self.frames, self.sent = frames, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def __aiter__(self):
        for frame in self.frames:
            yield json.dumps(frame)


def trade(symbol: str, price: float, stamp: str) -> dict:
    return {"T": "t", "S": symbol, "p": price, "s": 100, "t": stamp}


async def test_stream_authenticates_subscribes_and_keeps_the_extremes(_clock):
    _clock.set(at(10, 0, 30))
    socket = FakeSocket(
        [
            [{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "trades": ["BUY"]}],
            [
                trade("BUY", 50.1, "2026-11-10T15:00:01Z"),
                trade("BUY", 48.2, "2026-11-10T15:00:04Z"),
                trade("BUY", 49.6, "2026-11-10T15:00:08Z"),
                trade("OTHER", 10.0, "2026-11-10T15:00:08Z"),
            ],
        ]
    )
    sleeps = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    stream = AlpacaStream("key-id", "secret", connect=lambda url: socket, sleep=no_sleep)
    stream.subscribe(["BUY"])

    await stream.run(max_attempts=1)

    assert socket.sent == [
        {"action": "auth", "key": "key-id", "secret": "secret"},
        {"action": "subscribe", "trades": ["BUY"]},
    ]
    (tick,) = await stream.poll(["BUY"])
    # The last price, with the dip in between: the watcher will not miss it.
    assert (tick.price, tick.low, tick.high, tick.source) == (49.6, 48.2, 50.1, "alpaca:iex")
    assert await stream.poll(["BUY"]) == []  # nothing new since the last read


async def test_stream_follows_at_most_thirty_symbols(_clock):
    stream = AlpacaStream("k", "s", connect=lambda url: FakeSocket([]))

    stream.subscribe([f"T{i:02d}" for i in range(45)])

    assert len(stream._wanted) == 30  # noqa: SLF001


async def test_stream_reconnects_with_longer_waits_and_reports_why(_clock):
    sleeps = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def refuse(url: str):
        raise OSError("network unreachable")

    stream = AlpacaStream("k", "s", connect=refuse, sleep=no_sleep)

    await stream.run(max_attempts=3)

    assert sleeps == [2.0, 4.0, 8.0]
    assert not stream.healthy
    assert stream.problem == "sin conexión con el stream de precios (OSError)"

    rejected = AlpacaStream(
        "k", "s", sleep=no_sleep,
        connect=lambda url: FakeSocket([[{"T": "error", "code": 402, "msg": "auth failed"}]]),
    )  # fmt: skip
    await rejected.run(max_attempts=1)
    assert not rejected.healthy


async def test_the_feed_falls_back_to_polling_while_the_stream_is_down(make_runtime, _clock):
    _clock.set(at(10, 0, 5))
    payload = snapshots(BUY=(50.1, "2026-11-10T15:00:01Z"))
    polling = PollingFeed(
        AlpacaCollector(make_runtime(lambda r: httpx.Response(200, json=payload)))
    )
    stream = AlpacaStream("k", "s", connect=lambda url: FakeSocket([]))
    feed = FallbackFeed(stream, polling)

    assert feed.mode == "polling"
    assert [(t.ticker, t.price) for t in await feed.poll(["BUY"])] == [("BUY", 50.1)]

    stream.handle({"T": "success", "msg": "authenticated"})
    stream.handle(trade("BUY", 50.4, "2026-11-10T15:00:07Z"))
    assert feed.mode == "stream"
    assert [(t.ticker, t.price) for t in await feed.poll(["BUY"])] == [("BUY", 50.4)]
    # Silence for too long means the stream is not to be trusted any more.
    _clock.advance(timedelta(minutes=3))
    assert feed.mode == "polling"


# --- five-minute candles and triggers ----------------------------------------------------------


def test_candles_close_when_a_price_of_the_next_five_minutes_arrives():
    builder = CandleBuilder()

    def add(when, price, **extremes):
        return builder.add(Tick("BUY", price, when, **extremes))

    assert add(at(9, 46), 50.0) is None
    assert add(at(9, 47, 30), 50.3, low=49.8) is None
    assert add(at(9, 49, 59), 50.1) is None
    candle = add(at(9, 50, 5), 50.2)

    assert candle == Candle("BUY", at(9, 45), 50.0, 50.3, 49.8, 50.1)
    assert candle.end == at(9, 50)
    assert add(at(9, 51), 50.4) is None  # the 09:50 candle is still forming


@pytest.fixture
def plan_ids(engine) -> dict[str, int]:
    """Today's plan saved: BUY and TWO to buy, NEAR watched for its score."""
    mixed = [candidate("BUY", 85.0), candidate("TWO", 83.0), candidate("NEAR", 65.0),
             candidate("LOW", 40.0), candidate("LOW2", 39.0)]  # fmt: skip
    with session_scope(engine) as session:
        plan_store.save_plan(session, plan_for(mixed))
    with session_scope(engine) as session:
        return {idea.ticker: idea.id for idea in plan_store.todays_ideas(session, DAY)}


@pytest.fixture
def watched(engine, plan_ids) -> int:
    """BUY opened above its no-chase price: it is now watched, waiting for the price."""
    with session_scope(engine) as session:
        plan_store.recheck_open(session, CONFIG, DAY, {"BUY": 51.2})
    return plan_ids["BUY"]


def test_only_ideas_a_price_can_fix_are_watched_for_a_trigger(engine, watched):
    with session_scope(engine) as session:
        tickers = [idea.ticker for idea in watch_triggers.watched_ideas(session, DAY)]

    assert tickers == ["BUY"]  # NEAR is held back by its score, not by the price


@pytest.mark.parametrize(
    ("end", "close", "met"),
    [
        ((9, 50), 50.4, True),  # inside the zone, after the first fifteen minutes
        ((9, 50), 50.75, True),  # at the no-chase price
        ((9, 50), 50.76, False),  # above it: still chasing
        ((9, 50), 49.7, False),  # below the zone
        ((9, 40), 50.4, False),  # the plan does not enter in the first fifteen minutes
    ],
)
def test_trigger_is_a_five_minute_close_in_the_buyable_range(engine, watched, end, close, met):
    with session_scope(engine) as session:
        idea = session.get(models.Idea, watched)
        candle = Candle("BUY", at(*end) - timedelta(minutes=5), close, close, close, close)

        assert watch_triggers.trigger_met(idea, candle, CONFIG) is met


def test_a_trigger_turns_the_idea_into_a_purchase_with_the_size_worked_out_again(
    engine, watched, _clock
):
    _clock.set(at(9, 50))
    with session_scope(engine) as session:
        idea = session.get(models.Idea, watched)
        promoted, reason = watch_triggers.try_promote(session, CONFIG, idea, 49.9, clock.now())

    assert (promoted, reason) == (True, "se cumplió el disparador: vela de 5 min cerró en $49.90")
    with session_scope(engine) as session:
        idea = session.get(models.Idea, watched)
        # Risk 18.75 (no AI: half conviction) over 49.90 - 48.30 = 1.60 per share -> 11.
        assert (idea.verdict, idea.shares, idea.reason) == ("COMPRAR", 11, None)
        assert idea.amount_usd == pytest.approx(11 * 49.9)
        assert idea.ev_r == pytest.approx(
            0.5 * (3.5 / 1.6) - 0.5 - 2 * 49.9 * 0.0034 / 1.6, abs=1e-3
        )
        audit = session.scalars(
            select(models.StateTransition)
            .where(models.StateTransition.entity == "idea_verdict")
            .order_by(models.StateTransition.id.desc())
        ).first()
        assert (audit.from_state, audit.to_state, audit.actor) == ("VIGILAR", "COMPRAR", "system")
        # With TWO still proposed, both purchases together fit in the cash.
        buys = [i for i in plan_store.todays_ideas(session, DAY) if i.verdict == "COMPRAR"]
        assert sum(i.amount_usd for i in buys) <= 5000.0


def test_a_trigger_does_not_promote_what_no_longer_qualifies(engine, watched, _clock):
    _clock.set(at(9, 50))
    with session_scope(engine) as session:
        idea = session.get(models.Idea, watched)
        # At 50.40 the stop is further and the target closer: not enough expectancy.
        first = watch_triggers.try_promote(session, CONFIG, idea, 50.4, clock.now())
        again = watch_triggers.try_promote(session, CONFIG, idea, 49.9, clock.now())
        assert idea.verdict == "VIGILAR"

    assert first == (False, "a $50.40 la expectativa es de +0.05R; el mínimo es +0.15R")
    assert again == (False, "ya se revisó hace poco")  # not retried for half an hour
    _clock.advance(timedelta(minutes=31))
    with session_scope(engine) as session:
        idea = session.get(models.Idea, watched)
        assert watch_triggers.try_promote(session, CONFIG, idea, 49.9, clock.now())[0] is True


# --- AI review of a position -------------------------------------------------------------------


@pytest.fixture
def held(engine, plan_ids, _clock) -> int:
    """10 shares of BUY bought at 50.00 (stop 48.30, targets 53.40 and 55.10)."""
    _clock.set(at(9, 45))
    with session_scope(engine) as session:
        return confirmations.confirm_bought(session, plan_ids["BUY"], price=50.0, qty=10).id


def reviewer(**changes) -> SimulatedAI:
    def mutate(output, request):
        return {**copy.deepcopy(output), **changes}

    return SimulatedAI(mutate=mutate)


def ask(engine, ai, position_id, price, trigger=review.NEAR_TARGET, **kwargs):
    with session_scope(engine) as session:
        position = session.get(models.Position, position_id)
        return review.review_position(
            session, gateway(ai), CONFIG, position, price=price, at=clock.now(), trigger=trigger,
            **kwargs,
        )  # fmt: skip


def test_reasons_to_ask_the_ai_about_a_position(engine, held):
    with session_scope(engine) as session:
        view = watching.view_of(session.get(models.Position, held), DAY)

    assert review.due_triggers(view, 50.6) == []
    assert review.due_triggers(view, 48.5) == [review.NEAR_STOP]
    assert review.due_triggers(view, 53.2) == [review.NEAR_TARGET]
    assert review.due_triggers(view, 51.6, previous_close=50.0) == [review.MOVE]
    assert review.due_triggers(view, 50.6, has_news=True, closing=True) == [
        review.NEWS,
        review.CLOSE,
    ]


def test_position_dossier_and_a_hold_answer(engine, held):
    ai = SimulatedAI()

    advice = ask(engine, ai, held, 53.2)

    assert (advice.action, advice.new_stop) == ("MANTENER", None)
    assert review.advice_signal(advice, None) is None
    (call,) = ai.calls
    assert (call.task, call.model) == ("position_review", "sonnet")
    assert "# Revisión de posición · BUY" in call.dossier
    block = json.loads(call.dossier.split("```json\n")[1].split("\n```")[0])
    assert block["motivo_de_la_revision"] == "cerca del objetivo"
    assert block["posicion"]["resultado_en_r"] == pytest.approx(1.88)
    # The only stops the AI may propose: above the current one and below the price.
    assert block["niveles_validos"] == [
        {"precio": 50.0, "nivel": "tu precio de entrada"},
        {"precio": 52.2, "nivel": "1 ATR debajo del precio actual"},
    ]


def test_the_ai_can_ask_to_sell_or_to_raise_the_stop_to_a_valid_level(engine, held):
    sell = ask(engine, reviewer(accion="VENDER", motivo="perdió fuerza"), held, 53.2)
    with session_scope(engine) as session:
        view = watching.view_of(session.get(models.Position, held), DAY)
    signal = review.advice_signal(sell, view)
    assert (signal.rule, signal.action) == ("tesis_invalidada", "Vender")
    assert signal.explanation == "la IA recomienda salir: perdió fuerza"

    raised = ask(
        engine, reviewer(accion="SUBIR_STOP", nuevo_stop=50.01, motivo="proteger"), held, 53.2,
        trigger=review.MOVE,
    )  # fmt: skip
    signal = review.advice_signal(raised, view)
    assert raised.new_stop == 50.0  # snapped to the valid level it is next to
    assert (signal.rule, signal.new_stop) == ("stop_movil", 50.0)


def test_the_ai_cannot_invent_a_stop_or_lower_it(engine, held):
    invented = ask(engine, reviewer(accion="SUBIR_STOP", nuevo_stop=51.37), held, 53.2)
    lowered = ask(
        engine, reviewer(accion="SUBIR_STOP", nuevo_stop=47.0), held, 53.2, trigger=review.MOVE
    )

    assert (invented.action, invented.new_stop) == ("MANTENER", None)
    assert (lowered.action, lowered.new_stop) == ("MANTENER", None)


def test_the_ai_is_asked_once_a_day_per_reason_and_never_without_quota(engine, held, _clock):
    ai = SimulatedAI()

    assert ask(engine, ai, held, 53.2) is not None
    assert ask(engine, ai, held, 53.3) is None  # same reason, same day
    assert ask(engine, ai, held, 48.5, trigger=review.NEAR_STOP) is not None
    assert len(ai.calls) == 2
    _clock.advance(timedelta(days=1))
    assert ask(engine, ai, held, 53.2) is not None

    with session_scope(engine) as session:
        position = session.get(models.Position, held)
        out_of_quota = review.review_position(
            session, gateway(SimulatedAI(), daily_call_budget=0), CONFIG, position,
            price=53.2, at=clock.now(), trigger=review.CLOSE,
        )  # fmt: skip
    assert out_of_quota is None  # and the exit rules carry on without it


# --- the service through a day -----------------------------------------------------------------


class ScriptedFeed:
    """Prices set by the test: each poll returns the current price of what is asked."""

    def __init__(self) -> None:
        self.prices: dict[str, float] = {}
        self.polls: list[list[str]] = []
        self.problem = None

    async def poll(self, tickers):
        wanted = sorted(set(tickers))
        self.polls.append(wanted)
        return [Tick(t, self.prices[t], clock.now()) for t in wanted if t in self.prices]


DAY_2 = date(2026, 11, 11)  # the day the busy market's tests use


@pytest.fixture
def day(busy, _clock):
    feed, telegram = ScriptedFeed(), FakeTelegram()
    _clock.set(at(8, 50, day=DAY_2))

    def service() -> Service:
        bot = TelegramBot(busy, CONFIG, telegram, CHAT)
        return Service(busy, CONFIG, feed=feed, bot=bot)

    return service, feed, telegram, _clock, busy


def todays(engine) -> dict[str, models.Idea]:
    with session_scope(engine) as session:
        return {idea.ticker: idea for idea in plan_store.todays_ideas(session, DAY_2)}


async def test_the_daily_tasks_run_once_each_at_their_time(day):
    make, feed, telegram, moment, engine = day
    service = make()

    assert await service.run_due() == ["premarket"]  # 08:50: the plan, nothing else yet
    assert await service.run_due() == []
    assert telegram.sent == []
    moment.set(at(9, 0, 10, day=DAY_2))
    assert await service.run_due() == ["publish"]
    published = len(telegram.sent)
    assert telegram.sent[0].text.startswith("VERTICE · Plan del día · mié 11 nov 2026")
    assert published == 7  # header, five purchases with their buttons, the rest

    # 09:45: two purchases opened above their no-chase price.
    ideas = todays(engine)
    buys = [t for t, idea in ideas.items() if idea.verdict == "COMPRAR"]
    feed.prices = {t: ideas[t].entry_max for t in buys}
    feed.prices[buys[0]] = ideas[buys[0]].no_chase_above + 0.05
    moment.set(at(9, 45, 5, day=DAY_2))
    assert await service.run_due() == ["recheck"]
    assert todays(engine)[buys[0]].verdict == "VIGILAR"
    assert telegram.last.startswith(
        f"Revisión tras la apertura: {buys[0]} deja de ser COMPRAR y pasa a VIGILAR. Abrió en"
    )

    # A restart in the middle of the morning repeats nothing.
    moment.set(at(10, 30, day=DAY_2))
    restarted = make()
    sent = len(telegram.sent)
    assert await restarted.run_due() == []
    assert len(telegram.sent) == sent

    moment.set(at(15, 31, day=DAY_2))
    assert await restarted.run_due() == ["close_review"]
    reminders = [m for m in telegram.sent if m.text.startswith("Pendiente: ¿compraste")]
    assert len(reminders) == 4  # the purchases nobody answered

    moment.set(at(16, 16, day=DAY_2))
    assert await restarted.run_due() == ["close"]
    summary = next(m.text for m in telegram.sent if m.text.startswith("Cierre del 2026-11-11"))
    assert "capital $5,000.00" in summary
    assert "PROPOSED" not in {idea.state for idea in todays(engine).values()}
    assert await restarted.run_due() == []


async def test_nothing_is_scheduled_on_a_day_without_market(day):
    make, _, telegram, moment, _ = day
    moment.set(at(9, 0, day=date(2026, 11, 14)))  # a Saturday

    assert await make().run_due() == []
    assert telegram.sent == []


async def test_a_failing_task_is_retried_reported_and_does_not_stop_the_rest(day):
    make, feed, telegram, moment, engine = day

    def broken_analyst():
        raise RuntimeError("no se pudo preparar la IA")

    bot = TelegramBot(engine, CONFIG, telegram, CHAT)
    service = Service(engine, CONFIG, feed=feed, bot=bot, analyst_factory=broken_analyst)

    for _ in range(4):
        await service.run_due()
        moment.advance(timedelta(seconds=30))

    with session_scope(engine) as session:
        job = json.loads(session.get(models.SystemStatus, "job/premarket").message)
        assert session.get(models.SystemStatus, "job/premarket").status == "error"
    assert (job["ok"], job["attempts"]) == (False, 3)  # three tries, then it stops insisting
    assert telegram.last == (
        "PRECAUCIÓN: falló la tarea «Plan del día» 3 veces: no se pudo preparar la IA"
    )
    # The watcher keeps beating: the exit rules do not depend on the plan or the AI.
    moment.set(at(10, 0, day=DAY_2))
    assert (await service.watch_once())["ticks"] == 0


async def test_the_watcher_beat_alerts_once_and_waits_for_the_answer(day):
    make, feed, telegram, moment, engine = day
    service = make()
    await service.run_due()
    idea = next(i for i in todays(engine).values() if i.verdict == "COMPRAR")
    moment.set(at(9, 50, day=DAY_2))
    await service.bot.handle(press(f"buy:{idea.id}"))
    await service.bot.handle(say(f"{idea.entry_max} 10"))
    telegram.sent.clear()

    feed.prices = {idea.ticker: idea.stop + 0.5}
    moment.advance(timedelta(seconds=15))
    assert await service.watch_once() == {"ticks": 1, "alerts": 0, "promoted": 0, "sent": 0}
    assert idea.ticker in feed.polls[-1]

    feed.prices = {idea.ticker: idea.stop - 0.05}
    moment.advance(timedelta(seconds=15))
    report = await service.watch_once()
    assert (report["alerts"], report["sent"]) == (1, 1)
    assert telegram.last.splitlines()[0] == f"🔴 ALTA · {idea.ticker} · Stop"

    # The price stays there: no second alert, and a restarted service does not resend.
    for _ in range(5):
        moment.advance(timedelta(seconds=15))
        assert (await make().watch_once())["sent"] == 0
    with session_scope(engine) as session:
        (alert,) = watching.open_alerts(session)
        position = session.get(models.Position, alert.position_id)
        assert (position.state, position.qty_open) == ("SELL_ALERT", 10)  # never assumed sold
        feed_status = session.get(models.SystemStatus, "watcher/feed")
        assert feed_status.status == "ok"

    await service.bot.handle(press(f"sold:{alert.id}"))
    await service.bot.handle(say(f"{idea.stop - 0.05} 10"))
    assert telegram.last.startswith(f"Registrado: vendiste 10 {idea.ticker}")
    with session_scope(engine) as session:
        assert watching.open_alerts(session) == []


async def test_outside_the_session_the_watcher_does_not_ask_for_prices(day):
    make, feed, _, moment, _ = day
    service = make()
    moment.set(at(8, 0, day=DAY_2))

    assert (await service.watch_once())["ticks"] == 0
    assert feed.polls == []


async def test_a_watched_idea_is_promoted_when_its_five_minute_candle_confirms(day):
    make, feed, telegram, moment, engine = day
    service = make()
    await service.run_due()
    ideas = todays(engine)
    buys = [t for t, idea in ideas.items() if idea.verdict == "COMPRAR"]
    ticker = buys[0]
    # It opens above its no-chase price and is demoted at 09:45...
    feed.prices = {ticker: ideas[ticker].no_chase_above + 0.05}
    moment.set(at(9, 45, 5, day=DAY_2))
    await service.run_due()
    assert todays(engine)[ticker].verdict == "VIGILAR"
    telegram.sent.clear()

    # ...then comes back into its zone and a five-minute candle closes there.
    inside = ideas[ticker].entry_min + 0.01
    for minute, second in ((46, 0), (48, 0), (49, 50), (50, 5)):
        feed.prices = {ticker: inside}
        moment.set(at(9, minute, second, day=DAY_2))
        report = await service.watch_once()

    assert report["promoted"] == 1
    promoted = todays(engine)[ticker]
    assert promoted.verdict == "COMPRAR" and promoted.shares >= 1
    assert telegram.last.startswith(
        f"VIGILAR → COMPRAR: se cumplió el disparador de {ticker} (vela de 5 min cerró en"
    )
    assert telegram.sent[-1].buttons[0][0].data == f"buy:{promoted.id}"
    buys_now = [i for i in todays(engine).values() if i.verdict == "COMPRAR"]
    assert sum(i.amount_usd for i in buys_now) <= 5000.0


async def test_a_severe_headline_on_a_position_alerts_whatever_the_ai_thinks(day):
    from engine.schemas import NewsItem

    make, feed, telegram, moment, engine = day
    service = make()
    await service.run_due()
    idea = next(i for i in todays(engine).values() if i.verdict == "COMPRAR")
    moment.set(at(10, 0, day=DAY_2))
    with session_scope(engine) as session:
        confirmations.confirm_bought(session, idea.id, price=idea.entry_max, qty=10)
    moment.advance(timedelta(minutes=20))
    severe = NewsItem(
        tickers=(idea.ticker,), published_at=clock.now() - timedelta(minutes=2), source="alpaca",
        headline="SEC opens fraud investigation", url="https://example.com/n1", sentiment=-0.9,
        category="investigation", severe=True,
    )  # fmt: skip
    before = NewsItem(
        tickers=(idea.ticker,), published_at=clock.now() - timedelta(days=2), source="alpaca",
        headline="Old probe", url="https://example.com/n0", category="investigation", severe=True,
    )  # fmt: skip

    assert service.raise_news_alerts([severe, before], clock.now()) == 1
    assert service.raise_news_alerts([severe, before], clock.now()) == 0  # each headline once

    with session_scope(engine) as session:
        (alert,) = watching.open_alerts(session)
        assert (alert.rule, alert.urgency) == ("noticia_grave", "ALTA")
        assert alert.data["explanation"] == (
            "noticia grave: «SEC opens fraud investigation» (alpaca); sin opinión de la IA"
        )
