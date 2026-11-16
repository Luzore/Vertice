"""FastAPI application.

The API of the dashboard (session, main view, pages, settings, confirmations), the
WebSocket that tells the browser when something changed, and the built dashboard
itself, served as static files.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from app import __version__, live
from app.api import dashboard, plan, status
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
    live.feed.start()
    try:
        yield
    finally:
        await live.feed.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="VERTICE", version=__version__, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.include_router(status.router, prefix="/api", tags=["status"])
    app.include_router(dashboard.public, prefix="/api", tags=["session"])
    app.include_router(dashboard.router, prefix="/api", tags=["dashboard"])
    app.include_router(plan.router, prefix="/api", tags=["plan"])
    app.include_router(live.router, tags=["live"])

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

    _serve_dashboard(app)
    return app


NOT_BUILT = """<!doctype html><html lang="es"><meta charset="utf-8"><title>VERTICE</title>
<body style="font-family:system-ui;background:#0b1220;color:#e6ecf7;padding:3rem">
<h1>El dashboard todavía no está compilado</h1>
<p>Corre <code>make frontend</code> y recarga esta página. La API ya está funcionando.</p>
</body></html>"""


def dashboard_dir() -> Path:
    return settings.repo_root() / "frontend" / "dist"


def _serve_dashboard(app: FastAPI) -> None:
    """Serve the built dashboard: hashed assets cached for good, and the same page
    for every route of the dashboard (it is a single-page app)."""
    dist = dashboard_dir()
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.middleware("http")
    async def _cache_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/{path:path}", include_in_schema=False)
    async def _page(path: str):
        if path.startswith(("api/", "ws", "assets/")):
            raise HTTPException(404, "no existe")
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(NOT_BUILT)
        static = (dist / path).resolve()
        if path and static.is_file() and dist.resolve() in static.parents:
            return FileResponse(static)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()
