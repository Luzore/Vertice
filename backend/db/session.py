"""Engine and session factory.

SQLite runs in WAL mode with foreign keys enforced. Both are per-connection
settings in SQLite, so they are applied every time a connection is opened.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from core import settings


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def make_engine(url: str | None = None) -> Engine:
    """Create an engine for ``url`` (default: the configured database)."""
    url = url or settings.database_url()
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url)
        event.listen(engine, "connect", _apply_sqlite_pragmas)
        return engine
    return create_engine(url)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return make_engine()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextlib.contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """A session that commits on success and rolls back on any error."""
    session = make_session_factory(engine or get_engine())()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
