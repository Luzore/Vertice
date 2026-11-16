"""Access to the dashboard: one password, one signed session cookie.

Nothing of the dashboard is public (ROADMAP section 14). The password lives in
``.env`` as ``DASHBOARD_PASSWORD``; without it nobody can log in. A session is a
cookie the browser cannot read from scripts, signed with a key that only this
installation has, valid for thirty days.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as random
import time
from collections import deque

from fastapi import HTTPException, Request, WebSocket
from sqlalchemy import Engine

from core import clock, settings
from db import models
from db.session import get_engine, session_scope

COOKIE = "vertice_session"
SESSION_SECONDS = 30 * 24 * 3600
KEY_COMPONENT = "auth/session_key"
MAX_FAILURES, FAILURE_WINDOW_S = 5, 300

# Recent failed logins (monotonic seconds). Five in five minutes lock the login.
_failures: deque[float] = deque(maxlen=MAX_FAILURES)


def configured() -> bool:
    return settings.get_secrets().dashboard_password is not None


def _key(engine: Engine | None = None) -> bytes:
    """The signing key of this installation, created the first time it is needed."""
    with session_scope(engine or get_engine()) as session:
        row = session.get(models.SystemStatus, KEY_COMPONENT)
        if row is None:
            row = models.SystemStatus(
                component=KEY_COMPONENT,
                status="ok",
                message=random.token_hex(32),
                updated_at=clock.now(),
            )
            session.add(row)
        return bytes.fromhex(row.message)


def _sign(expires: int) -> str:
    return hmac.new(_key(), str(expires).encode(), hashlib.sha256).hexdigest()


def new_token() -> str:
    expires = int(time.time()) + SESSION_SECONDS
    return f"{expires}.{_sign(expires)}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, _, signature = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(int(expires)))


def locked_out() -> bool:
    now = time.monotonic()
    recent = [moment for moment in _failures if now - moment < FAILURE_WINDOW_S]
    return len(recent) >= MAX_FAILURES


def check_password(candidate: str) -> bool:
    """True when ``candidate`` is the dashboard password. Failures are counted."""
    password = settings.get_secrets().dashboard_password
    ok = password is not None and hmac.compare_digest(
        candidate.encode(), password.get_secret_value().encode()
    )
    if not ok:
        _failures.append(time.monotonic())
    return ok


def reset_failures() -> None:
    _failures.clear()


def require_session(request: Request) -> None:
    """Dependency of every protected route: a valid session, or 401."""
    if not configured():
        raise HTTPException(
            503, "falta DASHBOARD_PASSWORD en .env: sin contraseña nadie puede entrar"
        )
    if not valid_token(request.cookies.get(COOKIE)):
        raise HTTPException(401, "inicia sesión para continuar")


def socket_allowed(websocket: WebSocket) -> bool:
    return configured() and valid_token(websocket.cookies.get(COOKIE))
