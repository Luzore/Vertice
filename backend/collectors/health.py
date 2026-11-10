"""Health of each data source (principle 13).

Every fresh snapshot updates ``system_status``. From those rows the system knows
which sources work, which are not configured, and whether a critical one is down,
in which case the state is PRECAUCIÓN and the reason is spelled out for the UI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from collectors.base import RawSnapshot, SnapshotStatus
from collectors.store import SnapshotStore
from core import clock
from core.settings import SOURCE_NAMES, DataConfig, Secrets
from db import models
from db.session import session_scope

PREFIX = "source:"
# From best to worst; a source is as healthy as its worst dataset.
_SEVERITY = {"ok": 0, "slow": 1, "stale": 2, "error": 3}

DataState = Literal["NORMAL", "PRECAUCION"]


def _component(source: str, dataset: str | None = None) -> str:
    return f"{PREFIX}{source}/{dataset}" if dataset else f"{PREFIX}{source}"


def record_snapshot(session: Session, snapshot: RawSnapshot) -> None:
    """Remember how the latest request for this source and dataset went."""
    if snapshot.from_cache or snapshot.status is SnapshotStatus.NOT_CONFIGURED:
        return
    session.merge(
        models.SystemStatus(
            component=_component(snapshot.source, snapshot.dataset),
            status=snapshot.status.value,
            message=snapshot.message,
            updated_at=clock.now(),
        )
    )


def sync_configuration(session: Session, secrets: Secrets) -> None:
    """Mark sources without credentials as not configured, and clear the mark
    for sources that have them now."""
    for source in SOURCE_NAMES:
        missing = secrets.missing_for(source)
        if missing:
            session.merge(
                models.SystemStatus(
                    component=_component(source),
                    status=SnapshotStatus.NOT_CONFIGURED.value,
                    message=f"sin configurar: falta {', '.join(missing)}",
                    updated_at=clock.now(),
                )
            )
        else:
            session.execute(
                delete(models.SystemStatus).where(
                    models.SystemStatus.component == _component(source)
                )
            )


@dataclass(frozen=True)
class SourceHealth:
    source: str
    critical: bool
    # ok, slow, stale, error, not_configured, or unknown when nothing was requested yet.
    status: str
    message: str | None = None
    updated_at: datetime | None = None
    datasets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DataHealth:
    state: DataState
    # One sentence per problem, in Spanish, naming the source and the reason.
    problems: list[str]
    sources: list[SourceHealth]


def source_overview(session: Session, config: DataConfig) -> list[SourceHealth]:
    rows = session.scalars(
        select(models.SystemStatus).where(models.SystemStatus.component.like(f"{PREFIX}%"))
    ).all()
    by_source: dict[str, list[models.SystemStatus]] = {}
    for row in rows:
        source = row.component[len(PREFIX) :].split("/", 1)[0]
        by_source.setdefault(source, []).append(row)

    overview = []
    for source in SOURCE_NAMES:
        source_rows = by_source.get(source, [])
        critical = config.sources[source].critical
        not_configured = next((r for r in source_rows if "/" not in r.component), None)
        if not_configured is not None:
            overview.append(
                SourceHealth(
                    source=source,
                    critical=critical,
                    status=not_configured.status,
                    message=not_configured.message,
                    updated_at=not_configured.updated_at,
                )
            )
            continue
        dataset_rows = [r for r in source_rows if "/" in r.component]
        if not dataset_rows:
            overview.append(SourceHealth(source=source, critical=critical, status="unknown"))
            continue
        worst = max(dataset_rows, key=lambda r: _SEVERITY.get(r.status, 0))
        overview.append(
            SourceHealth(
                source=source,
                critical=critical,
                status=worst.status,
                message=worst.message,
                updated_at=max(r.updated_at for r in dataset_rows),
                datasets={r.component.split("/", 1)[1]: r.status for r in dataset_rows},
            )
        )
    return overview


def data_health(session: Session, config: DataConfig) -> DataHealth:
    """NORMAL, or PRECAUCION when a critical source is down, stale or not configured."""
    sources = source_overview(session, config)
    problems = []
    for item in sources:
        if not item.critical or item.status in ("ok", "slow", "unknown"):
            continue
        labels = {
            "error": "caída",
            "stale": "con datos viejos",
            "not_configured": "sin configurar",
        }
        reason = (item.message or "").removeprefix("sin configurar: ")
        detail = f": {reason}" if reason else ""
        problems.append(
            f"Fuente crítica {item.source} {labels.get(item.status, item.status)}{detail}"
        )
    return DataHealth(
        state="PRECAUCION" if problems else "NORMAL", problems=problems, sources=sources
    )


class DatabaseRecorder:
    """Recorder used in production: stores the snapshot and updates the source status.

    Database and file work runs in a worker thread so it never blocks the event loop.
    """

    def __init__(self, engine: Engine, store: SnapshotStore) -> None:
        self._engine = engine
        self._store = store

    def _record_sync(self, snapshot: RawSnapshot) -> None:
        self._store.save(snapshot)
        with session_scope(self._engine) as session:
            record_snapshot(session, snapshot)

    async def record(self, snapshot: RawSnapshot) -> None:
        await asyncio.to_thread(self._record_sync, snapshot)
