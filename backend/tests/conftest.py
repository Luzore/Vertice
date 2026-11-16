"""Shared fixtures. Tests never touch the real .env or the real database."""

from __future__ import annotations

import contextlib
import copy
from pathlib import Path

import httpx2 as httpx
import pytest
import yaml
from sqlalchemy import Engine

from collectors.base import CollectorRuntime, RawSnapshot
from core import settings
from db import migrate
from db import session as db_session

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_CONFIG = REPO_ROOT / "config" / "config.yaml"

SECRET_ENV_NAMES = [name.upper() for name in settings.Secrets.model_fields]


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each test starts with no credentials, a throwaway database path and clean caches."""
    for name in SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("VERTICE_ROOT", raising=False)
    monkeypatch.delenv("VERTICE_CONFIG", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'unused.db'}")
    monkeypatch.setattr(settings, "env_path", lambda: tmp_path / "no-such.env")
    settings.reset_caches()
    db_session.get_engine.cache_clear()
    yield
    settings.reset_caches()
    db_session.get_engine.cache_clear()


@pytest.fixture
def config_dict() -> dict:
    """The shipped config.yaml as a dict that a test can modify freely."""
    return copy.deepcopy(yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8")))


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict to a temporary file and return its path."""

    def _write(data: dict) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'vertice-test.db'}"


@pytest.fixture
def engine(db_url: str) -> Engine:
    """A file database built by running the real migrations, not create_all()."""
    migrate.upgrade(db_url)
    engine = db_session.make_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def market(engine: Engine) -> Engine:
    """A database with a synthetic market: 24 tickers and 300 sessions up to 2026-11-13."""
    from db import models
    from tests.test_market_state import _sessions, seed

    seed(engine, [f"T{i:02d}" for i in range(24)], _sessions(300))
    with db_session.session_scope(engine) as session:
        for row in session.query(models.Universe).all():
            row.market_cap_usd = 5e9
    return engine


@pytest.fixture
def busy(market, monkeypatch):
    """The synthetic market with eight breakout candidates built on its real prices,
    so the daily runs have purchases, rechecks and shadows to work with (a random
    walk on its own never passes the 22 controls)."""
    from dataclasses import replace

    from engine import daily
    from engine.indicators import Levels
    from engine.screener import Candidate
    from tests.test_screener_plan import STRONG

    real_indicators = daily.compute_indicators

    def indicators_with_levels(state, regime_config):
        result = real_indicators(state, regime_config)
        levels = {
            ticker: Levels(supports=(round(float(result.metrics.loc[ticker, "close"]) * 0.968, 2),))
            for ticker in list(result.metrics.index)[:8]
        }
        return replace(result, levels={**result.levels, **levels}, regime="ALCISTA")

    def breakouts(indicators, screener_config):
        candidates = []
        for rank, ticker in enumerate(list(indicators.metrics.index)[:8]):
            close = float(indicators.metrics.loc[ticker, "close"])
            metrics = {
                **STRONG, "close": close, "atr_14": close * 0.02, "atr_pct": 2.0,
                "sma_20": close * 0.992, "ema_21": close * 0.992, "sma_50": close * 0.92,
                "sma_200": close * 0.8, "prior_close_max_20d": close * 0.99, "extension_atr": 0.4,
            }  # fmt: skip
            candidates.append(
                Candidate(ticker, "RUPTURA", 90.0 - rank, {"tendencia": 25.0},
                          ["rompe máximo de 20 días"], metrics)
            )  # fmt: skip
        return candidates

    monkeypatch.setattr(daily, "compute_indicators", indicators_with_levels)
    monkeypatch.setattr(daily, "screen", breakouts)
    return market


TEST_PASSWORD = "clave-de-prueba-del-dashboard"


@contextlib.contextmanager
def logged_in_client(monkeypatch: pytest.MonkeyPatch, db_url: str, *, keys: bool = False):
    """The API on the test database, with the dashboard password set and a session
    open. With ``keys`` every data source has its (fake) credentials, so none of them
    counts as down."""
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", db_url)
    if keys:
        for name, value in TEST_SECRETS.items():
            monkeypatch.setenv(name.upper(), value)
    monkeypatch.setenv("DASHBOARD_PASSWORD", TEST_PASSWORD)
    settings.reset_caches()
    db_session.get_engine.cache_clear()
    auth.reset_failures()
    migrate.upgrade(db_url)
    with TestClient(create_app()) as client:
        assert client.post("/api/login", json={"password": TEST_PASSWORD}).status_code == 200
        yield client


# --- collectors ---------------------------------------------------------------------


class FakeTime:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class MemoryRecorder:
    def __init__(self) -> None:
        self.snapshots: list[RawSnapshot] = []

    async def record(self, snapshot: RawSnapshot) -> None:
        self.snapshots.append(snapshot)


def max_calls_in_any_window(times: list[float], period_s: float) -> int:
    """Largest number of calls that fall inside any window of ``period_s`` seconds."""
    times = sorted(times)
    worst, start = 0, 0
    for end, moment in enumerate(times):
        while times[start] <= moment - period_s:
            start += 1
        worst = max(worst, end - start + 1)
    return worst


TEST_SECRETS = {
    "alpaca_api_key": "PKTESTKEY0000000000000000",
    "alpaca_api_secret": "test-alpaca-secret-0123456789abcdef",
    "finnhub_api_key": "test-finnhub-key-0123456789",
    "fred_api_key": "0123456789abcdef0123456789abcdef",
    "sec_user_agent": "VERTICE Test test@example.com",
    "telegram_bot_token": "1234567890:AAExampleTokenValue_abcDEF-123456789",
    "telegram_chat_id": "424242424",
    "massive_api_key": "test-massive-key-0123456789",
    "banxico_token": "test-banxico-token-0123456789abcdef",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def data_config() -> settings.DataConfig:
    return settings.load_config(SHIPPED_CONFIG).data


@pytest.fixture
def all_secrets() -> settings.Secrets:
    return settings.Secrets(_env_file=None, **TEST_SECRETS)


@pytest.fixture
def no_secrets() -> settings.Secrets:
    return settings.Secrets(_env_file=None)


@pytest.fixture
def recorder() -> MemoryRecorder:
    return MemoryRecorder()


@pytest.fixture
def make_runtime(data_config, all_secrets, fake_time, recorder):
    """Build a collector runtime whose HTTP calls are answered by ``handler``."""

    def _make(handler, *, secrets: settings.Secrets | None = None) -> CollectorRuntime:
        return CollectorRuntime(
            config=data_config,
            secrets=secrets if secrets is not None else all_secrets,
            recorder=recorder,
            transport=httpx.MockTransport(handler),
            sleep=fake_time.sleep,
            monotonic=fake_time.monotonic,
            jitter=lambda: 0.0,
        )

    return _make
