"""The system at work through the day (ROADMAP section 16).

One object ties the pieces together: collectors, the plan, Telegram, the watcher
and the close. The scheduler calls it on a timer; the tests call it directly.

* ``run_due`` runs each daily task once, when its time has come: collect, plan,
  publish, recheck, review before the close, close. What ran is remembered in the
  database, so a restart neither repeats a task nor skips one that is still due.
* ``watch_once`` is the watcher's heartbeat during the session: new prices, exit
  rules, triggers of the watched ideas, alerts.
* A task that fails is recorded and retried; it never stops the others. The exit
  rules need neither the AI nor Telegram to keep working (principle 6).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Engine, select

from collectors.alpaca import AlpacaCollector
from collectors.base import CollectorRuntime, SnapshotStatus
from collectors.price_feed import PriceFeed
from core import clock, market_calendar
from core.logging import get_logger
from core.settings import AppConfig
from db import models
from db.session import session_scope
from engine import account, daily, exits, plan_store, runtime_config, states, watch_triggers
from engine import data_engine as de
from engine import position_review as review
from engine import watcher as watching
from engine.ai_analyst import Analyst
from engine.ai_gateway import AIGateway
from engine.ingest import ingest_daily_bars, ingest_earnings_calendar, ingest_macro
from engine.universe import CONTEXT_TICKERS
from engine.watcher import Watcher
from notify import messages as msg
from notify.telegram import TelegramBot

log = get_logger(__name__)

JOB_PREFIX = "job/"
FEED_STATUS = "watcher/feed"
MAX_ATTEMPTS = 3
NEWS_EVERY = timedelta(minutes=5)


@dataclass(frozen=True)
class DailyTask:
    name: str
    # What it is, in Spanish (for the status and for failure messages).
    label: str
    due: Callable[[market_calendar.Session], datetime]
    until: Callable[[market_calendar.Session], datetime]
    run: Callable[[], Awaitable[str | None]]


def _at(session: market_calendar.Session, moment: time) -> datetime:
    return clock.market_time(session.day, moment)


class Service:
    def __init__(
        self,
        engine: Engine,
        config: AppConfig,
        *,
        feed: PriceFeed,
        bot: TelegramBot | None = None,
        runtime: CollectorRuntime | None = None,
        analyst_factory: Callable[[], Analyst | None] | None = None,
        gateway_factory: Callable[[], AIGateway | None] | None = None,
    ) -> None:
        self.engine, self.config = engine, config
        self.feed, self.bot, self.runtime = feed, bot, runtime
        self._analyst_factory = analyst_factory or (lambda: None)
        self._gateway_factory = gateway_factory or (lambda: None)
        self.watcher = Watcher(engine, config)
        self.candles = watch_triggers.CandleBuilder()
        self._news_checked_at: datetime | None = None
        plan = config.plan
        half_hour, quarter = timedelta(minutes=30), timedelta(minutes=15)
        self.tasks = [
            DailyTask("collect", "colectores nocturnos", lambda s: _at(s, time(6, 0)),
                      lambda s: _at(s, time(8, 40)), self.collect),
            DailyTask("premarket", "Plan del día", lambda s: _at(s, time(8, 45)),
                      lambda s: s.close - timedelta(hours=1), self.premarket),
            DailyTask("publish", "publicación del plan", lambda s: _at(s, plan.publish_time_et),
                      lambda s: s.close, self.publish),
            DailyTask("recheck", "revisión tras la apertura",
                      lambda s: _at(s, plan.recheck_time_et), lambda s: s.close - half_hour,
                      self.recheck),
            DailyTask("close_review", "revisión de cierre", lambda s: s.close - half_hour,
                      lambda s: s.close, self.close_review),
            DailyTask("close", "cierre del día", lambda s: s.close + quarter,
                      lambda s: _at(s, time(23, 59)), self.close),
        ]  # fmt: skip

    # --- bookkeeping of the daily tasks --------------------------------------------------

    def _job(self, name: str) -> dict:
        with session_scope(self.engine) as session:
            row = session.get(models.SystemStatus, f"{JOB_PREFIX}{name}")
            return json.loads(row.message) if row is not None and row.message else {}

    def _record(self, name: str, day: date, *, ok: bool, detail: str | None, attempts: int) -> None:
        with session_scope(self.engine) as session:
            key = f"{JOB_PREFIX}{name}"
            row = session.get(models.SystemStatus, key)
            if row is None:
                row = models.SystemStatus(component=key, status="ok", updated_at=clock.now())
                session.add(row)
            row.status = "ok" if ok else "error"
            row.message = json.dumps(
                {"day": day.isoformat(), "ok": ok, "detail": detail, "attempts": attempts},
                ensure_ascii=False,
            )
            row.updated_at = clock.now()

    def _done_today(self, name: str, day: date) -> bool:
        job = self._job(name)
        if job.get("day") != day.isoformat():
            return False
        return bool(job.get("ok")) or job.get("attempts", 0) >= MAX_ATTEMPTS

    async def _tell(self, text: str) -> None:
        if self.bot is not None:
            await self.bot._say(text)  # noqa: SLF001 - the bot's own plain message

    def refresh_config(self) -> None:
        """Pick up what the user changed from the dashboard (mode, aggressiveness...)."""
        config = runtime_config.current(self.engine)
        if config != self.config:
            self.config = config
            self.watcher._config = config  # noqa: SLF001
            if self.bot is not None:
                self.bot._config = config  # noqa: SLF001

    async def run_due(self, now: datetime | None = None) -> list[str]:
        """Run every daily task whose time has come and that has not run today.
        Returns the names of the tasks that ran."""
        self.refresh_config()
        now = now or clock.now()
        session = market_calendar.get_session(clock.market_date(now))
        if session is None:
            return []  # no market today: nothing is scheduled
        ran = []
        for task in self.tasks:
            if not task.due(session) <= now <= task.until(session):
                continue
            if self._done_today(task.name, session.day):
                continue
            if task.name in ("publish", "recheck") and not self._job("premarket").get("ok"):
                continue  # these need today's plan
            previous = self._job(task.name)
            attempts = (
                previous.get("attempts", 0) if previous.get("day") == session.day.isoformat() else 0
            ) + 1
            try:
                detail = await task.run()
            except Exception as error:  # a failed task must not stop the others
                log.error("task_failed", task=task.name, error=type(error).__name__)
                self._record(
                    task.name, session.day, ok=False, detail=str(error)[:300], attempts=attempts
                )
                if attempts >= MAX_ATTEMPTS:
                    await self._tell(
                        f"PRECAUCIÓN: falló la tarea «{task.label}» {attempts} veces: "
                        f"{str(error)[:200]}"
                    )
            else:
                self._record(task.name, session.day, ok=True, detail=detail, attempts=attempts)
            ran.append(task.name)
        return ran

    # --- the daily tasks -----------------------------------------------------------------

    async def collect(self) -> str | None:
        """Bars of the last sessions, macro series and the earnings calendar."""
        if self.runtime is None:
            return "sin colectores configurados"
        with session_scope(self.engine) as session:
            tickers = list(
                session.scalars(
                    select(models.Universe.ticker).where(models.Universe.active.is_(True))
                )
            )
        start = clock.market_date() - timedelta(days=7)
        bars = await ingest_daily_bars(
            self.runtime, self.engine, [*tickers, *CONTEXT_TICKERS], start=start
        )
        macro = await ingest_macro(self.runtime, self.engine, self.config)
        earnings = await ingest_earnings_calendar(self.runtime, self.engine)
        return f"{bars} barras, {macro} datos macro, {earnings} fechas de resultados"

    async def premarket(self) -> str | None:
        analyst = self._analyst_factory()
        run_id, plan = await asyncio.to_thread(
            daily.run_premarket, self.engine, self.config, analyst=analyst
        )
        with session_scope(self.engine) as session:
            rotations = (plan_store.plan_as_dict(session, plan.plan_date) or {}).get("rotations")
        self.watcher.suggest_rotations(rotations or [])
        return f"corrida {run_id}: {len(plan.by_verdict('COMPRAR'))} para comprar"

    async def publish(self) -> str | None:
        if self.bot is None:
            return "sin Telegram configurado"
        sent = await self.bot.publish_plan()
        await self.bot.send_alerts(self.watcher)
        return f"{sent} mensajes"

    async def recheck(self) -> str | None:
        day = clock.market_date(clock.now())
        with session_scope(self.engine) as session:
            buys = [
                idea.ticker
                for idea in plan_store.todays_ideas(session, day)
                if idea.verdict == "COMPRAR" and idea.state == "PROPOSED"
            ]
        opening = {tick.ticker: tick.price for tick in await self.feed.poll(buys)}
        for ticker in buys:
            known = self.watcher.last_prices.get(ticker)
            if ticker not in opening and known is not None:
                opening[ticker] = known.price
        changed = daily.run_recheck(self.engine, self.config, opening_prices=opening, day=day)
        for ticker, verdict, reason in changed:
            await self._tell(
                f"Revisión tras la apertura: {ticker} deja de ser COMPRAR y pasa a "
                f"{verdict.replace('_', ' ')}. {reason[:1].upper()}{reason[1:]}."
            )
        return f"{len(changed)} cambios con {len(opening)} precios de apertura"

    async def close_review(self) -> str | None:
        now = clock.now()
        day = clock.market_date(now)
        next_day = market_calendar.next_trading_day(day)
        tickers = self.watcher.watched_tickers()
        for tick in await self.feed.poll(tickers):
            self.watcher.on_tick(tick, regime=self._regime(day))
        prices = {t: tick.price for t, tick in self.watcher.last_prices.items()}
        with session_scope(self.engine) as session:
            events = de.load_corporate_events(session, now, tickers)
        reporting = [
            t for t in tickers if exits.reports_before_next_open(events.get(t, ()), day, next_day)
        ]
        created = self.watcher.close_review(prices, as_of=now, reports_soon=reporting)
        self._ai_reviews(prices, now, closing=True)
        reminders = 0
        if self.bot is not None:
            await self.bot.send_alerts(self.watcher)
            reminders = await self.bot.remind_pending(day)
        return f"{len(created)} alertas de cierre, {reminders} recordatorios"

    async def close(self) -> str | None:
        day = clock.market_date(clock.now())
        if self.runtime is not None:
            with session_scope(self.engine) as session:
                tickers = {idea.ticker for idea in plan_store.todays_ideas(session, day)}
                tickers |= {
                    p.ticker
                    for p in session.scalars(
                        select(models.Position).where(models.Position.state != "CLOSED")
                    )
                }
            await ingest_daily_bars(self.runtime, self.engine, [*tickers, "SPY"], start=day)
        summary = await asyncio.to_thread(daily.run_close, self.engine, self.config, day=day)
        if summary.note:
            raise RuntimeError(summary.note)
        lines = [f"Cierre del {day.isoformat()}: capital ${summary.equity:,.2f}."]
        if summary.day_pnl is not None:
            lines[0] += f" Resultado del día: {msg.signed(summary.day_pnl)} USD."
        for label, key in (
            ("Sombras nuevas", "shadow"),
            ("No ejecutables", "not_fillable"),
            ("Sin tu respuesta (pasaron a sombra o expiraron)", "expired"),
        ):
            if summary.ideas.get(key):
                lines.append(f"{label}: {', '.join(summary.ideas[key])}.")
        for ticker, reasons in summary.shadow_sales.items():
            lines.append(f"Sombra {ticker}: {'; '.join(reasons)}.")
        await self._tell("\n".join(lines))
        if self.bot is not None:
            await self.bot.remind_pending(day)
        return lines[0]

    # --- the watcher's heartbeat ---------------------------------------------------------

    def _regime(self, day: date) -> str:
        with session_scope(self.engine) as session:
            run_id = plan_store.plan_run_id(session, day)
            return (session.get(models.Run, run_id).regime or "") if run_id is not None else ""

    def _feed_status(self, ok: bool, message: str) -> None:
        with session_scope(self.engine) as session:
            row = session.get(models.SystemStatus, FEED_STATUS)
            if row is None:
                row = models.SystemStatus(
                    component=FEED_STATUS, status="ok", updated_at=clock.now()
                )
                session.add(row)
            row.status, row.message, row.updated_at = (
                "ok" if ok else "degradado",
                message,
                clock.now(),
            )

    async def watch_once(self, now: datetime | None = None) -> dict[str, int]:
        """One beat of the watcher: read new prices, apply the exit rules, check the
        watched ideas and send what is due. Does nothing outside the session."""
        self.refresh_config()
        now = now or clock.now()
        report = {"ticks": 0, "alerts": 0, "promoted": 0, "sent": 0}
        if market_calendar.market_status(now) != "open":
            if self.bot is not None:
                report["sent"] = await self.bot.send_alerts(self.watcher)
            return report
        day = clock.market_date(now)
        held = self.watcher.watched_tickers()
        with session_scope(self.engine) as session:
            watched = {idea.ticker for idea in watch_triggers.watched_ideas(session, day)}
        ticks = await self.feed.poll(sorted(set(held) | watched))
        problem = getattr(self.feed, "problem", None)
        self._feed_status(problem is None, problem or getattr(self.feed, "mode", "ok"))
        regime = self._regime(day)
        for tick in ticks:
            report["ticks"] += 1
            if tick.ticker in held:
                report["alerts"] += len(self.watcher.on_tick(tick, regime=regime))
            else:
                self.watcher.last_prices[tick.ticker] = tick
            candle = self.candles.add(tick)
            if candle is not None and tick.ticker in watched:
                report["promoted"] += await self._check_trigger(candle, day, now)
        if ticks:
            prices = {tick.ticker: tick.price for tick in ticks if tick.ticker in held}
            report["alerts"] += self._ai_reviews(prices, now)
        await self._check_news(held, now)
        if self.bot is not None:
            report["sent"] = await self.bot.send_alerts(self.watcher)
        return report

    async def _check_trigger(self, candle: watch_triggers.Candle, day: date, now: datetime) -> int:
        promoted = 0
        with states.guarded_session(self.engine) as session:
            for idea in watch_triggers.watched_ideas(session, day):
                if idea.ticker != candle.ticker:
                    continue
                if not watch_triggers.trigger_met(idea, candle, self.config):
                    continue
                done, _ = watch_triggers.try_promote(session, self.config, idea, candle.close, now)
                if done:
                    promoted += 1
                    session.flush()
                    data = plan_store.idea_as_dict(idea)
                    capital = account.load_portfolio(session, self.config).capital
                    text = (
                        f"VIGILAR → COMPRAR: se cumplió el disparador de {idea.ticker} "
                        f"(vela de 5 min cerró en ${candle.close:,.2f}; no perseguir sobre "
                        f"${idea.no_chase_above:,.2f}). Tamaño recalculado:\n\n"
                        + msg.idea_message(data, capital).text
                    )
                    if self.bot is not None:
                        await self.bot._transport.send(  # noqa: SLF001
                            msg.OutMessage(text, msg.idea_buttons(idea.id))
                        )
        return promoted

    def _ai_reviews(self, prices: dict[str, float], now: datetime, *, closing: bool = False) -> int:
        """Ask the AI about the positions that have a reason for it today. Without a
        gateway, or without quota, this does nothing and the rules carry on."""
        gateway = self._gateway_factory()
        if gateway is None or not prices:
            return 0
        raised = 0
        today = clock.market_date(now)
        with states.guarded_session(self.engine) as session:
            closes = account.last_prices(session, prices, now)
            for position in account.real_positions(session, open_only=True):
                price = prices.get(position.ticker)
                if price is None:
                    continue
                view = watching.view_of(position, today)
                triggers = review.due_triggers(
                    view, price, previous_close=closes.get(position.ticker), closing=closing
                )
                asked = (position.tracking or {}).get("ai_reviews") or {}
                pending = [t for t in triggers if asked.get(t) != today.isoformat()]
                if not pending:
                    continue
                advice = review.review_position(
                    session, gateway, self.config, position, price=price, at=now, trigger=pending[0]
                )
                signal = review.advice_signal(advice, view) if advice is not None else None
                if signal is not None and watching.raise_alert(
                    session, position, signal, price=price, source="revisión de la IA", at=now
                ):
                    raised += 1
        return raised

    async def _check_news(self, held: list[str], now: datetime) -> int:
        """Rule 2: download the news of the held tickers every few minutes and raise a
        high-urgency alert for each severe headline not alerted before."""
        if self.runtime is None or not held:
            return 0
        if self._news_checked_at is not None and now - self._news_checked_at < NEWS_EVERY:
            return 0
        since = self._news_checked_at or now - timedelta(
            hours=self.config.controls.severe_news_hours
        )
        self._news_checked_at = now
        snapshot = await AlpacaCollector(self.runtime).news(held, start=since)
        if snapshot.status not in (SnapshotStatus.OK, SnapshotStatus.SLOW):
            return 0
        items = de.parse_alpaca_news(snapshot.payload)
        with session_scope(self.engine) as session:
            de.store_news(session, items)
        return self.raise_news_alerts(items, now)

    def raise_news_alerts(self, items: list, now: datetime) -> int:
        """Alert on the severe headlines among ``items`` that concern an open position
        and were published after it was opened. Each headline alerts once."""
        gateway = self._gateway_factory()
        raised = 0
        today = clock.market_date(now)
        with states.guarded_session(self.engine) as session:
            for position in account.real_positions(session, open_only=True):
                tracking = dict(position.tracking or {})
                alerted = list(tracking.get("news_alerted") or [])
                fresh = [
                    item
                    for item in items
                    if position.ticker in item.tickers
                    and item.severe
                    and item.published_at >= position.opened_at
                    and (item.url or item.headline) not in alerted
                ]
                if not fresh:
                    continue
                item = max(fresh, key=lambda n: n.published_at)
                tracking["news_alerted"] = [*alerted, *((n.url or n.headline) for n in fresh)]
                position.tracking = tracking
                price = tracking.get("last_price") or position.avg_price
                advice = None
                if gateway is not None:
                    advice = review.review_position(
                        session, gateway, self.config, position,
                        price=price, at=now, trigger=review.NEWS, news=fresh,
                    )  # fmt: skip
                signal = review.severe_news_signal(watching.view_of(position, today), item, advice)
                if watching.raise_alert(
                    session, position, signal, price=price, source="último precio", at=now
                ):
                    raised += 1
        return raised

    async def telegram_once(self) -> int:
        return await self.bot.poll_once() if self.bot is not None else 0
