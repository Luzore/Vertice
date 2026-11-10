"""Alembic environment.

The URL comes from ``sqlalchemy.url`` when a caller sets it (tests do), otherwise
from ``DATABASE_URL`` / the default data directory. Connections are opened through
``db.session.make_engine`` so migrations run with the same SQLite settings as the app.
"""

from __future__ import annotations

from alembic import context

from core import settings
from db.models import Base
from db.session import make_engine
from db.types import JSONText, UTCDateTime

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.database_url()


def _render_item(type_, obj, autogen_context):
    # Migrations must not depend on application code: write the project's custom
    # column types as the plain SQL type they are stored as.
    if type_ == "type" and isinstance(obj, UTCDateTime | JSONText):
        return "sa.Text()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                compare_type=True,
                render_item=_render_item,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
