from __future__ import annotations

from datetime import date, time

import pytest
from fastapi.testclient import TestClient

from app.api import status
from app.main import create_app
from core import clock
from db import migrate


@pytest.fixture
def client(db_url, monkeypatch):
    """The API running against a freshly migrated database and without any key."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    migrate.upgrade(db_url)
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_reports_ok_without_any_api_key(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["system"] == "VERTICE"
    assert body["mode"] == "recommend"
    assert body["clock"] == "real"
    assert body["database"] == {"status": "ok", "schema_revision": "0001"}
    assert body["market"] in {"open", "premarket", "closed"}
    assert body["time"]["user_timezone"] == "America/Monterrey"


def test_health_shows_the_three_time_zones(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    migrate.upgrade(db_url)

    with clock.simulated(clock.market_time(date(2026, 12, 15), time(9, 0))):
        body = status.health()

    assert body["clock"] == "simulated"
    assert body["time"]["utc"] == "2026-12-15T14:00:00+00:00"
    assert body["time"]["new_york"] == "2026-12-15T09:00:00-05:00"
    assert body["time"]["user"] == "2026-12-15T08:00:00-06:00"
    assert body["market"] == "premarket"


def test_health_is_degraded_when_the_database_is_not_migrated(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)  # empty database: no migrations applied

    with TestClient(create_app()) as test_client:
        body = test_client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert body["database"] == {"status": "needs_migration", "schema_revision": None}


def test_startup_fails_loudly_on_an_invalid_config(tmp_path, monkeypatch):
    bad_config = tmp_path / "config.yaml"
    bad_config.write_text("system: {name: VERTICE}\n", encoding="utf-8")
    monkeypatch.setenv("VERTICE_CONFIG", str(bad_config))

    with pytest.raises(Exception, match="is invalid"), TestClient(create_app()):
        pass
