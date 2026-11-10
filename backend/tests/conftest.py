"""Shared fixtures. Tests never touch the real .env or the real database."""

from __future__ import annotations

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
