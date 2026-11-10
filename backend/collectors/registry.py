"""Builds the collector runtime the application uses."""

from __future__ import annotations

import httpx2 as httpx
from sqlalchemy import Engine

from collectors.base import CollectorRuntime
from collectors.health import DatabaseRecorder
from collectors.store import SnapshotStore
from core import settings
from core.settings import AppConfig, Secrets


def raw_dir():
    return settings.data_dir() / "raw"


def build_runtime(
    config: AppConfig,
    secrets: Secrets,
    *,
    engine: Engine | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CollectorRuntime:
    """Runtime for live use. With ``engine``, every snapshot is stored and the
    status of its source is updated; without it nothing is persisted."""
    recorder = None
    if engine is not None:
        recorder = DatabaseRecorder(engine, SnapshotStore(engine, raw_dir()))
    return CollectorRuntime(
        config=config.data,
        secrets=secrets,
        recorder=recorder,
        fomc_dates=tuple(config.fomc_dates),
        transport=transport,
    )
