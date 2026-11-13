"""FastAPI application.

The health check, the status of the data sources, the plan of the day, the
account and the manual confirmations. The live WebSocket and the dashboard arrive
in later phases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api import plan, status
from collectors.health import sync_configuration
from core import settings
from core.logging import configure_logging, get_logger
from db.session import get_engine, session_scope
from engine.confirmations import ConfirmationError, NotFound
from engine.plan_store import PlanLocked
from engine.states import InvalidTransition


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    secrets = settings.get_secrets()
    configure_logging(secret_values=secrets.secret_values())
    # Loading the config here makes an invalid config.yaml stop the server at startup.
    config = settings.get_config()
    get_logger(__name__).info(
        "startup",
        system=config.system.name,
        version=__version__,
        mode=config.system.mode,
        sources_configured=secrets.source_status(),
    )
    # Sources without credentials are marked as not configured. The server starts
    # either way; a database that is not migrated yet is reported by /api/health.
    try:
        with session_scope(get_engine()) as session:
            sync_configuration(session, secrets)
    except SQLAlchemyError as exc:
        get_logger(__name__).warning("source_status_not_recorded", error=str(exc))
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="VERTICE", version=__version__, lifespan=lifespan)
    app.include_router(status.router, prefix="/api", tags=["status"])
    app.include_router(plan.router, prefix="/api", tags=["plan"])

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, error: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(ConfirmationError)
    async def _bad_confirmation(request: Request, error: ConfirmationError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(PlanLocked)
    async def _plan_locked(request: Request, error: PlanLocked) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(InvalidTransition)
    async def _invalid_transition(request: Request, error: InvalidTransition) -> JSONResponse:
        # Nothing was changed and the incident is already recorded: the system is in
        # PRECAUCIÓN until the user resolves it.
        return JSONResponse(
            {
                "detail": str(error),
                "system_state": "PRECAUCION",
                "incident": f"incident/{error.entity}/{error.entity_id}",
            },
            status_code=409,
        )

    return app


app = create_app()
