"""State machine of ideas and positions (ROADMAP section 11).

Every change of state goes through ``transition``: it checks that the move is
valid, writes it to ``state_transitions`` with the time and who made it
(principle 7), and only then changes the row.

An invalid move is never applied. It raises ``InvalidTransition``; the caller's
transaction is rolled back and the incident is recorded as PRECAUCIÓN in
``system_status`` until the user resolves it in the UI.

Ideas::

    PROPOSED -> BOUGHT | DECLINED | EXPIRED
    DECLINED -> SHADOW | NOT_FILLABLE          EXPIRED -> SHADOW | NOT_FILLABLE

Positions::

    OPEN -> SELL_ALERT -> PARTIALLY_CLOSED -> CLOSED

Corrections by the user (a late "la compré", or undoing one entered by mistake)
are extra moves, allowed only when ``correction=True`` and always audited.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Literal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from core import clock
from core.logging import get_logger
from db import models
from db.session import session_scope

log = get_logger(__name__)

Entity = Literal["idea", "position"]
Actor = Literal["system", "user"]

IDEA_TRANSITIONS: dict[str, frozenset[str]] = {
    "PROPOSED": frozenset({"BOUGHT", "DECLINED", "EXPIRED"}),
    "DECLINED": frozenset({"SHADOW", "NOT_FILLABLE"}),
    "EXPIRED": frozenset({"SHADOW", "NOT_FILLABLE"}),
    "BOUGHT": frozenset(),
    "SHADOW": frozenset(),
    "NOT_FILLABLE": frozenset(),
}
# Extra moves a correction by the user may make.
IDEA_CORRECTIONS: dict[str, frozenset[str]] = {
    "DECLINED": frozenset({"BOUGHT"}),
    "EXPIRED": frozenset({"BOUGHT"}),
    "SHADOW": frozenset({"BOUGHT"}),
    "NOT_FILLABLE": frozenset({"BOUGHT"}),
    "BOUGHT": frozenset({"DECLINED"}),
}
POSITION_TRANSITIONS: dict[str, frozenset[str]] = {
    "OPEN": frozenset({"SELL_ALERT", "PARTIALLY_CLOSED", "CLOSED"}),
    "SELL_ALERT": frozenset({"OPEN", "PARTIALLY_CLOSED", "CLOSED"}),
    "PARTIALLY_CLOSED": frozenset({"SELL_ALERT", "CLOSED"}),
    "CLOSED": frozenset(),
}
_TABLES: dict[str, dict[str, frozenset[str]]] = {
    "idea": IDEA_TRANSITIONS,
    "position": POSITION_TRANSITIONS,
}

INCIDENT_PREFIX = "incident/"
CAUTION = "PRECAUCION"
RESOLVED = "resuelto"


class InvalidTransition(Exception):
    """A change of state the machine does not allow. Nothing was changed."""

    def __init__(self, entity: str, entity_id: int | str, from_state: str, to_state: str) -> None:
        self.entity, self.entity_id = entity, str(entity_id)
        self.from_state, self.to_state = from_state, to_state
        name = "la idea" if entity == "idea" else "la posición"
        super().__init__(
            f"transición inválida: {name} {entity_id} está en {from_state} y no puede pasar "
            f"a {to_state}"
        )


def can_transition(
    entity: Entity, from_state: str, to_state: str, *, correction: bool = False
) -> bool:
    allowed = _TABLES[entity].get(from_state, frozenset())
    if correction and entity == "idea":
        allowed = allowed | IDEA_CORRECTIONS.get(from_state, frozenset())
    return to_state in allowed


def record_creation(
    session: Session,
    entity: Entity,
    row: models.Idea | models.Position,
    *,
    actor: Actor,
    reason: str,
) -> None:
    """Audit the first state of a new idea or position."""
    session.add(
        models.StateTransition(
            entity=entity,
            entity_id=str(row.id),
            from_state=None,
            to_state=row.state,
            at=clock.now(),
            actor=actor,
            reason=reason,
        )
    )


def transition(
    session: Session,
    entity: Entity,
    row: models.Idea | models.Position,
    to_state: str,
    *,
    actor: Actor,
    reason: str,
    correction: bool = False,
) -> None:
    """Move ``row`` to ``to_state`` and audit it, or raise ``InvalidTransition``."""
    from_state = row.state
    if not can_transition(entity, from_state, to_state, correction=correction):
        raise InvalidTransition(entity, row.id, from_state, to_state)
    session.add(
        models.StateTransition(
            entity=entity,
            entity_id=str(row.id),
            from_state=from_state,
            to_state=to_state,
            at=clock.now(),
            actor=actor,
            reason=f"corrección: {reason}" if correction else reason,
        )
    )
    row.state = to_state


def record_incident(session: Session, error: InvalidTransition) -> str:
    """Put the system in PRECAUCIÓN because of an invalid transition. Returns the
    name of the incident, which the user resolves in the UI."""
    component = f"{INCIDENT_PREFIX}{error.entity}/{error.entity_id}"
    row = session.get(models.SystemStatus, component)
    if row is None:
        row = models.SystemStatus(component=component, status=CAUTION, updated_at=clock.now())
        session.add(row)
    row.status, row.message, row.updated_at = CAUTION, str(error), clock.now()
    log.error("invalid_transition", component=component, detail=str(error))
    return component


@contextlib.contextmanager
def guarded_session(engine: Engine) -> Iterator[Session]:
    """A transaction in which an invalid transition changes nothing and leaves the
    system in PRECAUCIÓN."""
    try:
        with session_scope(engine) as session:
            yield session
    except InvalidTransition as error:
        with session_scope(engine) as session:
            record_incident(session, error)
        raise


def open_incidents(session: Session) -> list[models.SystemStatus]:
    return list(
        session.scalars(
            select(models.SystemStatus)
            .where(
                models.SystemStatus.component.startswith(INCIDENT_PREFIX),
                models.SystemStatus.status == CAUTION,
            )
            .order_by(models.SystemStatus.updated_at)
        )
    )


def resolve_incident(session: Session, component: str, note: str = "") -> bool:
    """Mark an incident as resolved by the user. False when there is no such incident."""
    row = session.get(models.SystemStatus, component)
    if row is None or not component.startswith(INCIDENT_PREFIX) or row.status != CAUTION:
        return False
    row.status, row.updated_at = RESOLVED, clock.now()
    row.message = f"{row.message} — resuelto: {note}" if note else f"{row.message} — resuelto"
    return True
