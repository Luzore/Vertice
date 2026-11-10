from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from collectors.base import RawSnapshot, SnapshotStatus
from collectors.health import (
    DatabaseRecorder,
    data_health,
    record_snapshot,
    source_overview,
    sync_configuration,
)
from collectors.store import SnapshotStore
from core import clock
from db import models
from db.session import session_scope

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def _snapshot(**changes) -> RawSnapshot:
    base = RawSnapshot(
        source="alpaca",
        dataset="daily_bars",
        request_key="symbols=SPY",
        as_of=NOW,
        fetched_at=NOW,
        status=SnapshotStatus.OK,
        latency_ms=182,
        payload={"bars": {"SPY": [{"c": 748.58, "nota": "cañón ñ"}]}},
    )
    return replace(base, **changes)


@pytest.fixture
def store(engine, tmp_path) -> SnapshotStore:
    return SnapshotStore(engine, tmp_path / "raw")


# --- raw snapshot store ------------------------------------------------------------


def test_snapshot_is_stored_as_a_compressed_file_plus_an_index_row(store, engine, tmp_path):
    snapshot = _snapshot()

    row_id = store.save(snapshot)

    with session_scope(engine) as session:
        row = session.get(models.RawSnapshot, row_id)
        assert (row.source, row.dataset, row.request_key) == ("alpaca", "daily_bars", "symbols=SPY")
        assert (row.status, row.latency_ms, row.as_of) == ("ok", 182, NOW)
        assert row.payload_path.startswith("2026-11-10/alpaca/daily_bars__150000__")
        path, sha, size = row.payload_path, row.payload_sha256, row.payload_bytes
    stored_file = tmp_path / "raw" / path
    body = gzip.decompress(stored_file.read_bytes())
    assert json.loads(body) == snapshot.payload
    assert len(body) == size
    assert len(sha) == 64
    assert stored_file.stat().st_size > 0


def test_latest_returns_the_payload_back(store):
    store.save(_snapshot())

    stored = store.latest("alpaca", "daily_bars", "symbols=SPY", as_of=NOW)

    assert stored.status == "ok"
    assert store.load_payload(stored) == _snapshot().payload
    assert store.latest("alpaca", "daily_bars", "symbols=QQQ", as_of=NOW) is None
    assert store.latest("finnhub", "daily_bars", as_of=NOW) is None


def test_latest_never_returns_data_from_the_future_of_the_clock(store):
    """Replay safety: with a simulated clock, later snapshots are invisible."""
    morning = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
    afternoon = datetime(2026, 5, 4, 19, 0, tzinfo=UTC)
    store.save(_snapshot(as_of=morning, payload={"when": "morning"}))
    store.save(_snapshot(as_of=afternoon, payload={"when": "afternoon"}))

    with clock.simulated(datetime(2026, 5, 4, 15, 0, tzinfo=UTC)):
        seen = store.latest("alpaca", "daily_bars")
        assert store.load_payload(seen) == {"when": "morning"}

    with clock.simulated(datetime(2026, 5, 4, 12, 0, tzinfo=UTC)):
        assert store.latest("alpaca", "daily_bars") is None

    assert store.load_payload(store.latest("alpaca", "daily_bars", as_of=afternoon)) == {
        "when": "afternoon"
    }


def test_errors_are_indexed_without_a_file(store, engine, tmp_path):
    store.save(_snapshot(status=SnapshotStatus.ERROR, payload=None, message="sin respuesta"))

    with session_scope(engine) as session:
        row = session.scalars(select(models.RawSnapshot)).one()
        assert (row.status, row.message, row.payload_path) == ("error", "sin respuesta", None)
    assert not (tmp_path / "raw").exists()
    assert store.latest("alpaca", "daily_bars", as_of=NOW) is None  # nothing usable


def test_cached_and_not_configured_snapshots_are_not_stored(store, engine):
    assert store.save(_snapshot(from_cache=True)) is None
    assert store.save(_snapshot(status=SnapshotStatus.NOT_CONFIGURED, payload=None)) is None

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM raw_snapshots")).scalar() == 0


def test_purge_removes_rows_and_files_older_than_the_retention(store, engine, tmp_path):
    old = NOW - timedelta(days=31)
    recent = NOW - timedelta(days=29)
    store.save(_snapshot(as_of=old, fetched_at=old))
    store.save(_snapshot(as_of=recent, fetched_at=recent))

    removed = store.purge(retention_days=30, now=NOW)

    assert removed == 1
    remaining = sorted(
        p.relative_to(tmp_path / "raw").parts[0] for p in (tmp_path / "raw").rglob("*.gz")
    )
    assert remaining == [f"{recent:%Y-%m-%d}"]
    assert not (tmp_path / "raw" / f"{old:%Y-%m-%d}").exists()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM raw_snapshots")).scalar() == 1
    assert store.purge(retention_days=30, now=NOW) == 0


# --- source health ---------------------------------------------------------------------


def test_unknown_until_something_is_requested(engine, data_config):
    with session_scope(engine) as session:
        health = data_health(session, data_config)

    assert health.state == "NORMAL"
    assert {item.status for item in health.sources} == {"unknown"}
    assert [item.source for item in health.sources if item.critical] == [
        "alpaca",
        "finnhub",
        "fred",
    ]


def test_critical_source_down_means_precaucion_and_says_why(engine, data_config):
    with session_scope(engine) as session:
        record_snapshot(session, _snapshot())
        record_snapshot(
            session,
            _snapshot(
                source="finnhub",
                dataset="earnings_calendar",
                status=SnapshotStatus.ERROR,
                message="finnhub.io respondió HTTP 503 (error del servidor), tras 5 intentos",
            ),
        )

    with session_scope(engine) as session:
        health = data_health(session, data_config)

    assert health.state == "PRECAUCION"
    assert health.problems == [
        "Fuente crítica finnhub caída: finnhub.io respondió HTTP 503 (error del servidor), "
        "tras 5 intentos"
    ]


def test_stale_critical_data_also_means_precaucion(engine, data_config):
    with session_scope(engine) as session:
        record_snapshot(
            session,
            _snapshot(
                status=SnapshotStatus.STALE, message="la última barra diaria es del 2026-11-06"
            ),
        )
        health = data_health(session, data_config)

    assert health.state == "PRECAUCION"
    assert health.problems[0].startswith("Fuente crítica alpaca con datos viejos: la última barra")


def test_optional_source_down_does_not_change_the_state(engine, data_config):
    with session_scope(engine) as session:
        record_snapshot(
            session,
            _snapshot(source="yahoo", dataset="chart", status=SnapshotStatus.ERROR, message="429"),
        )
        record_snapshot(session, _snapshot(status=SnapshotStatus.SLOW, message="respuesta lenta"))
        health = data_health(session, data_config)

    assert health.state == "NORMAL"
    assert health.problems == []


def test_source_is_as_healthy_as_its_worst_dataset_and_recovers(engine, data_config):
    with session_scope(engine) as session:
        record_snapshot(session, _snapshot(dataset="latest_quotes"))
        record_snapshot(
            session, _snapshot(dataset="daily_bars", status=SnapshotStatus.ERROR, message="caída")
        )
        alpaca = next(s for s in source_overview(session, data_config) if s.source == "alpaca")
        assert alpaca.status == "error"
        assert alpaca.datasets == {"latest_quotes": "ok", "daily_bars": "error"}

    with session_scope(engine) as session:
        record_snapshot(session, _snapshot(dataset="daily_bars"))  # the next request works
        assert data_health(session, data_config).state == "NORMAL"


def test_sources_without_credentials_are_marked_and_later_cleared(
    engine, data_config, no_secrets, all_secrets
):
    with session_scope(engine) as session:
        sync_configuration(session, no_secrets)
        health = data_health(session, data_config)

    by_source = {item.source: item for item in health.sources}
    assert by_source["alpaca"].status == "not_configured"
    assert by_source["alpaca"].message == "sin configurar: falta ALPACA_API_KEY, ALPACA_API_SECRET"
    assert by_source["rss_news"].status == "unknown"  # needs no key
    assert health.state == "PRECAUCION"
    assert health.problems[0] == (
        "Fuente crítica alpaca sin configurar: falta ALPACA_API_KEY, ALPACA_API_SECRET"
    )

    with session_scope(engine) as session:
        sync_configuration(session, all_secrets)
        health = data_health(session, data_config)
    assert health.state == "NORMAL"
    assert {item.status for item in health.sources} == {"unknown"}


@pytest.mark.anyio
async def test_database_recorder_stores_and_updates_status(engine, store, data_config):
    recorder = DatabaseRecorder(engine, store)

    await recorder.record(_snapshot())
    await recorder.record(
        _snapshot(dataset="latest_quotes", status=SnapshotStatus.STALE, message="viejo")
    )

    assert (
        store.load_payload(store.latest("alpaca", "daily_bars", as_of=NOW)) == _snapshot().payload
    )
    with session_scope(engine) as session:
        alpaca = next(s for s in source_overview(session, data_config) if s.source == "alpaca")
    assert alpaca.datasets == {"daily_bars": "ok", "latest_quotes": "stale"}
