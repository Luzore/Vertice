"""Live updates for the dashboard (ROADMAP section 14).

The browser keeps one WebSocket open. When anything changes in the database, from
this process or from the worker, every open dashboard is told and asks for fresh
data: a change shows up in well under two seconds without reloading.

How a change is noticed: SQLite gives each connection a counter (``PRAGMA
data_version``) that moves whenever *another* connection commits. One idle
connection here reads it twice a second. Other databases get a plain timer.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.engine import make_url

from app import auth
from core import clock, settings
from core.logging import get_logger

log = get_logger(__name__)
router = APIRouter()

POLL_SECONDS = 0.5
FALLBACK_SECONDS = 2.0


class ChangeFeed:
    """Tells every connected dashboard when the database changed."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        self._task: asyncio.Task | None = None
        self.changes = 0

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._sockets.add(websocket)
        await websocket.send_json({"type": "hello", "at": clock.now().isoformat()})

    def disconnect(self, websocket: WebSocket) -> None:
        self._sockets.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        for websocket in list(self._sockets):
            try:
                await websocket.send_json(message)
            except Exception:  # a browser that went away
                self._sockets.discard(websocket)

    async def _watch(self) -> None:
        url = make_url(settings.database_url())
        connection = None
        if url.get_backend_name() == "sqlite" and url.database not in (None, ":memory:"):
            connection = sqlite3.connect(url.database, check_same_thread=False)
        last = None
        try:
            while True:
                if connection is not None:
                    await asyncio.sleep(POLL_SECONDS)
                    try:
                        version = connection.execute("PRAGMA data_version").fetchone()[0]
                    except sqlite3.Error:
                        continue
                    if last is not None and version != last:
                        self.changes += 1
                        await self.broadcast({"type": "changed", "at": clock.now().isoformat()})
                    last = version
                else:
                    await asyncio.sleep(FALLBACK_SECONDS)
                    await self.broadcast({"type": "changed", "at": clock.now().isoformat()})
        finally:
            if connection is not None:
                connection.close()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


feed = ChangeFeed()


@router.websocket("/ws")
async def live(websocket: WebSocket) -> None:
    """The dashboard's live channel. It only says "something changed"; the data
    itself is always read through the API, with the session checked there too."""
    if not auth.socket_allowed(websocket):
        await websocket.close(code=4401)
        return
    await feed.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # the browser only sends keep-alives
    except WebSocketDisconnect:
        pass
    finally:
        feed.disconnect(websocket)
