from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime

import pytest

from core import clock
from core.logging import REDACTED, SecretRedactor, configure_logging, get_logger

FRED_KEY = "2f0a9c1b7d6e4f3a8b5c0d9e1f2a3b4c"
TELEGRAM_TOKEN = "1234567890:AAExampleTokenValue_abcDEF-123456789"
ALPACA_SECRET = "s3cr3tAlpacaValue0123456789ABCDEFGHIJ"
ALL_SECRETS = [FRED_KEY, TELEGRAM_TOKEN, ALPACA_SECRET]


@pytest.fixture
def log_stream():
    """Logging configured as in production, writing to a buffer the test can read."""
    stream = io.StringIO()
    configure_logging("DEBUG", secret_values=ALL_SECRETS, stream=stream)
    yield stream
    logging.getLogger().handlers.clear()


def _records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_each_line_is_a_json_object(log_stream):
    get_logger("collectors.fred").info("fetched", series="VIXCLS", latency_ms=182)

    (record,) = _records(log_stream)
    assert record["event"] == "fetched"
    assert record["level"] == "info"
    assert record["logger"] == "collectors.fred"
    assert record["series"] == "VIXCLS"
    assert record["latency_ms"] == 182
    assert datetime.fromisoformat(record["timestamp"]).utcoffset().total_seconds() == 0


def test_known_secret_values_are_removed_wherever_they_appear(log_stream):
    get_logger().warning(
        f"request failed with key {FRED_KEY}",
        detail={"nested": [f"again {ALPACA_SECRET}"]},
        note=ALPACA_SECRET,
    )

    output = log_stream.getvalue()
    for secret in ALL_SECRETS:
        assert secret not in output
    (record,) = _records(log_stream)
    assert record["event"] == f"request failed with key {REDACTED}"
    assert record["detail"] == {"nested": [f"again {REDACTED}"]}


def test_fields_named_like_credentials_are_hidden_even_if_the_value_is_unknown(log_stream):
    get_logger().info(
        "request",
        headers={"X-Finnhub-Token": "unknown-value-1", "APCA-API-SECRET-KEY": "unknown-value-2"},
        api_key="unknown-value-3",
        telegram_bot_token="unknown-value-4",
        user_agent="VERTICE Someone someone@example.com",
    )

    output = log_stream.getvalue()
    assert "unknown-value" not in output
    assert "someone@example.com" not in output


def test_usage_counters_are_not_mistaken_for_secrets(log_stream):
    get_logger().info("ai_call", usage={"input_tokens": 1200, "output_tokens": 340})

    (record,) = _records(log_stream)
    assert record["usage"] == {"input_tokens": 1200, "output_tokens": 340}


def test_keys_inside_urls_are_removed(log_stream):
    get_logger().error(
        "http_error",
        fred="https://api.stlouisfed.org/fred/series/observations?series_id=VIXCLS&api_key=not-in-the-known-list&file_type=json",
        telegram="https://api.telegram.org/bot99999:unknownTokenValue_xyz/sendMessage",
        header="Authorization: Bearer abcdefghijklmnop.qrstuv",
    )

    (record,) = _records(log_stream)
    assert record["fred"].endswith(f"series_id=VIXCLS&api_key={REDACTED}&file_type=json")
    assert record["telegram"] == f"https://api.telegram.org/bot{REDACTED}/sendMessage"
    assert record["header"] == f"Authorization: Bearer {REDACTED}"


def test_secrets_in_exception_tracebacks_are_removed(log_stream):
    try:
        raise ConnectionError(f"GET https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe failed")
    except ConnectionError:
        get_logger().exception("telegram_failed")

    output = log_stream.getvalue()
    assert TELEGRAM_TOKEN not in output
    (record,) = _records(log_stream)
    assert "ConnectionError" in record["exception"]


def test_third_party_loggers_are_redacted_too(log_stream):
    logging.getLogger("httpx").info(
        "HTTP Request: GET https://api.stlouisfed.org/fred/series?api_key=%s", FRED_KEY
    )

    output = log_stream.getvalue()
    assert FRED_KEY not in output
    (record,) = _records(log_stream)
    assert record["logger"] == "httpx"
    assert f"api_key={REDACTED}" in record["event"]


def test_objects_are_logged_as_redacted_text(log_stream):
    error = ValueError(f"bad response for {ALPACA_SECRET}")

    get_logger().error("failed", error=error)

    (record,) = _records(log_stream)
    assert record["error"] == f"bad response for {REDACTED}"


def test_simulated_time_is_recorded_during_replay(log_stream):
    replay_time = datetime(2026, 5, 4, 13, 30, tzinfo=UTC)

    with clock.simulated(replay_time):
        get_logger().info("inside_replay")
    get_logger().info("outside_replay")

    inside, outside = _records(log_stream)
    assert inside["sim_time"] == replay_time.isoformat()
    assert "sim_time" not in outside


def test_short_values_are_not_used_for_matching():
    redactor = SecretRedactor(["42", "abc"])  # e.g. a short chat id

    assert redactor(None, "info", {"event": "page 42 of abc"}) == {"event": "page 42 of abc"}


def test_web_server_logs_go_through_the_same_redacted_stream():
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.StreamHandler(io.StringIO()))  # what uvicorn sets up itself
    access.propagate = False
    stream = io.StringIO()

    configure_logging("INFO", secret_values=ALL_SECRETS, stream=stream)
    access.info('127.0.0.1 - "GET /api/health?token=%s HTTP/1.1" 200', FRED_KEY)

    (record,) = _records(stream)
    assert record["logger"] == "uvicorn.access"
    assert FRED_KEY not in stream.getvalue()
    logging.getLogger().handlers.clear()
