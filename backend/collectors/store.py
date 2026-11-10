"""Storage of raw snapshots: a compressed file per payload plus an index row.

Snapshots are kept ``raw_retention_days`` (30) for replay and audit, then purged.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, delete, select

from collectors.base import RawSnapshot, SnapshotStatus
from core import clock
from db import models
from db.session import session_scope


@dataclass(frozen=True)
class StoredSnapshot:
    id: int
    source: str
    dataset: str
    request_key: str
    as_of: datetime
    fetched_at: datetime
    status: str
    latency_ms: int | None
    message: str | None
    payload_path: str | None


def _encode(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class SnapshotStore:
    def __init__(self, engine: Engine, raw_dir: Path) -> None:
        self._engine = engine
        self._raw_dir = Path(raw_dir)

    def save(self, snapshot: RawSnapshot) -> int | None:
        """Store a fresh snapshot. Cached and not-configured ones are not stored."""
        if snapshot.from_cache or snapshot.status is SnapshotStatus.NOT_CONFIGURED:
            return None
        payload_path = payload_sha256 = payload_bytes = None
        if snapshot.payload is not None:
            body = _encode(snapshot.payload)
            payload_sha256 = hashlib.sha256(body).hexdigest()
            payload_bytes = len(body)
            relative = (
                Path(f"{snapshot.as_of:%Y-%m-%d}")
                / snapshot.source
                / f"{snapshot.dataset}__{snapshot.as_of:%H%M%S}__{payload_sha256[:16]}.json.gz"
            )
            target = self._raw_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # mtime=0 keeps the file identical for identical payloads.
            target.write_bytes(gzip.compress(body, mtime=0))
            payload_path = relative.as_posix()
        with session_scope(self._engine) as session:
            row = models.RawSnapshot(
                source=snapshot.source,
                dataset=snapshot.dataset,
                request_key=snapshot.request_key,
                as_of=snapshot.as_of,
                fetched_at=snapshot.fetched_at,
                latency_ms=snapshot.latency_ms,
                status=snapshot.status.value,
                message=snapshot.message,
                payload_path=payload_path,
                payload_sha256=payload_sha256,
                payload_bytes=payload_bytes,
            )
            session.add(row)
            session.flush()
            return row.id

    def latest(
        self,
        source: str,
        dataset: str,
        key: str | None = None,
        *,
        as_of: datetime | None = None,
    ) -> StoredSnapshot | None:
        """Newest snapshot with data that was requested at or before ``as_of``.

        ``as_of`` defaults to the active clock, so in replay this never returns a
        snapshot from the simulated future.
        """
        as_of = clock.ensure_utc(as_of) if as_of is not None else clock.now()
        query = (
            select(models.RawSnapshot)
            .where(
                models.RawSnapshot.source == source,
                models.RawSnapshot.dataset == dataset,
                models.RawSnapshot.as_of <= as_of,
                models.RawSnapshot.payload_path.is_not(None),
            )
            .order_by(models.RawSnapshot.as_of.desc(), models.RawSnapshot.id.desc())
            .limit(1)
        )
        if key is not None:
            query = query.where(models.RawSnapshot.request_key == key)
        with session_scope(self._engine) as session:
            row = session.scalars(query).first()
            if row is None:
                return None
            return StoredSnapshot(
                id=row.id,
                source=row.source,
                dataset=row.dataset,
                request_key=row.request_key,
                as_of=row.as_of,
                fetched_at=row.fetched_at,
                status=row.status,
                latency_ms=row.latency_ms,
                message=row.message,
                payload_path=row.payload_path,
            )

    def load_payload(self, stored: StoredSnapshot) -> Any:
        if stored.payload_path is None:
            return None
        return json.loads(gzip.decompress((self._raw_dir / stored.payload_path).read_bytes()))

    def purge(self, *, retention_days: int, now: datetime | None = None) -> int:
        """Delete snapshots fetched more than ``retention_days`` ago. Returns how many."""
        now = clock.ensure_utc(now) if now is not None else clock.now()
        cutoff = now - timedelta(days=retention_days)
        with session_scope(self._engine) as session:
            old = session.execute(
                select(models.RawSnapshot.id, models.RawSnapshot.payload_path).where(
                    models.RawSnapshot.fetched_at < cutoff
                )
            ).all()
            for _, payload_path in old:
                if payload_path:
                    (self._raw_dir / payload_path).unlink(missing_ok=True)
            if old:
                session.execute(
                    delete(models.RawSnapshot).where(
                        models.RawSnapshot.id.in_([row_id for row_id, _ in old])
                    )
                )
        self._remove_empty_dirs()
        return len(old)

    def _remove_empty_dirs(self) -> None:
        if not self._raw_dir.exists():
            return
        for directory in sorted(self._raw_dir.rglob("*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
