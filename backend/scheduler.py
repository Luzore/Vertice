"""``python -m scheduler``: the process that keeps the system running through the day.

It runs three things on a timer, in New York time (ROADMAP section 16):

* every 30 seconds, the daily tasks that are due (collect, plan, publish, recheck,
  review before the close, close);
* every 10 seconds, one beat of the watcher (prices, exit rules, alerts);
* every 3 seconds, the answers the user gave in Telegram.

Nothing here keeps state of its own: stop it and start it again and it carries on
from what the database says.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import Engine

from collectors.alpaca import AlpacaCollector
from collectors.price_feed import AlpacaStream, FallbackFeed, PollingFeed
from collectors.registry import build_runtime
from core import clock, settings
from core.logging import configure_logging, get_logger
from core.settings import AppConfig
from db.session import make_engine
from engine.ai_analyst import build_gateway, live_analyst, runs_dir
from engine.ai_gateway import AIGateway
from engine.service import Service
from notify.telegram import TelegramBot, TelegramClient

log = get_logger(__name__)

DUE_EVERY_S = 30
WATCH_EVERY_S = 10
TELEGRAM_EVERY_S = 3


class DailyGateway:
    """One AI gateway per market day for the position reviews: its "rules only"
    switch, set after two failures, lasts for that day and no longer."""

    def __init__(self, config: AppConfig, engine: Engine) -> None:
        self._config, self._engine = config, engine
        self._day: date | None = None
        self._gateway: AIGateway | None = None

    def __call__(self) -> AIGateway | None:
        if not self._config.ai.enabled:
            return None
        today = clock.market_date(clock.now())
        if self._day != today:
            self._day = today
            self._gateway = build_gateway(
                self._config, engine=self._engine, dossier_dir=runs_dir() / "dossiers"
            )
        return self._gateway


def build_service(config: AppConfig, engine: Engine) -> tuple[Service, AlpacaStream | None, list]:
    """The service as it runs live, with whatever is configured in ``.env``."""
    secrets = settings.get_secrets()
    runtime = build_runtime(config, secrets, engine=engine)
    # Live prices are read every few seconds: they are not stored as raw snapshots.
    price_runtime = build_runtime(config, secrets)
    ready = secrets.source_status()
    stream = None
    if ready["alpaca"]:
        stream = AlpacaStream(
            secrets.alpaca_api_key.get_secret_value(), secrets.alpaca_api_secret.get_secret_value()
        )
    feed = FallbackFeed(stream, PollingFeed(AlpacaCollector(price_runtime)))
    bot = None
    if config.notify.telegram and ready["telegram"]:
        client = TelegramClient(runtime)
        bot = TelegramBot(engine, config, client, client.chat_id)
    service = Service(
        engine,
        config,
        feed=feed,
        bot=bot,
        runtime=runtime,
        analyst_factory=lambda: live_analyst(config, engine) if config.ai.enabled else None,
        gateway_factory=DailyGateway(config, engine),
    )
    return service, stream, [runtime, price_runtime]


def build_scheduler(service: Service, config: AppConfig) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.system.timezone_market)
    for job, seconds in (
        (service.run_due, DUE_EVERY_S),
        (service.watch_once, WATCH_EVERY_S),
        (service.telegram_once, TELEGRAM_EVERY_S),
    ):
        # One at a time: a slow run is not piled upon, the missed beats are dropped.
        scheduler.add_job(
            job, "interval", seconds=seconds, max_instances=1, coalesce=True, id=job.__name__
        )
    return scheduler


async def serve() -> None:
    config = settings.get_config()
    configure_logging(secret_values=settings.get_secrets().secret_values())
    engine = make_engine()
    service, stream, runtimes = build_service(config, engine)
    scheduler = build_scheduler(service, config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    scheduler.start()
    stream_task = asyncio.create_task(stream.run()) if stream is not None else None
    log.info(
        "worker_started",
        telegram=service.bot is not None,
        price_stream=stream is not None,
        watching=service.watcher.watched_tickers(),
    )
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        if stream_task is not None:
            stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
        for runtime in runtimes:
            await runtime.aclose()
        log.info("worker_stopped")


def main() -> int:
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
