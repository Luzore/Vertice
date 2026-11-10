"""System status endpoints."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from alembic.migration import MigrationContext
from fastapi import APIRouter
from sqlalchemy import text

from app import __version__
from collectors.health import data_health
from core import clock, market_calendar, settings
from core.logging import get_logger
from db.migrate import head_revision
from db.session import get_engine, session_scope

router = APIRouter()
log = get_logger(__name__)


def _database_status() -> dict[str, str | None]:
    """``ok`` only when the database answers and its schema is fully migrated."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            revision = MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:  # report the failure instead of crashing the health check
        log.error("database_check_failed", error=str(exc))
        return {"status": "error", "schema_revision": None}
    if revision != head_revision():
        return {"status": "needs_migration", "schema_revision": revision}
    return {"status": "ok", "schema_revision": revision}


@router.get("/health")
def health() -> dict:
    """Liveness check: version, clock in the three time zones, market and database."""
    config = settings.get_config()
    moment = clock.now()
    database = _database_status()
    return {
        "status": "ok" if database["status"] == "ok" else "degraded",
        "system": config.system.name,
        "version": __version__,
        "mode": config.system.mode,
        "clock": "simulated" if clock.is_simulated() else "real",
        "time": {
            "utc": moment.isoformat(),
            "new_york": clock.to_market(moment).isoformat(),
            "user": clock.to_user(moment, ZoneInfo(config.system.timezone_user)).isoformat(),
            "user_timezone": config.system.timezone_user,
        },
        "market": market_calendar.market_status(moment),
        "database": database,
    }


@router.get("/status/sources")
def sources() -> dict:
    """Health of every data source, and whether a critical one is down."""
    config = settings.get_config()
    with session_scope(get_engine()) as session:
        health = data_health(session, config.data)
    return {
        "state": health.state,
        "problems": health.problems,
        "sources": [
            {
                "source": item.source,
                "critical": item.critical,
                "status": item.status,
                "message": item.message,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                "datasets": item.datasets,
            }
            for item in health.sources
        ],
    }
