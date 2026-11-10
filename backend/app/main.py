"""FastAPI application.

Phase 0 exposes only the health check. The plan, positions, confirmations and the
live WebSocket arrive in later phases.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import status
from core import settings
from core.logging import configure_logging, get_logger


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
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="VERTICE", version=__version__, lifespan=lifespan)
    app.include_router(status.router, prefix="/api", tags=["status"])
    return app


app = create_app()
