"""Shared fixtures. Tests never touch the real .env or the real database."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine

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
