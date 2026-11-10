"""Run Alembic migrations from code (startup checks, tests, tooling)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config(url: str | None = None) -> Config:
    """Alembic configuration that works from any working directory."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "db" / "migrations"))
    if url:
        config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | None, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def head_revision() -> str | None:
    """The newest revision in the migrations folder."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()
