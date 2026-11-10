"""Structured logging: one JSON object per line, with secrets scrubbed.

Principle 12: secrets are never logged. Two providers put the key where it leaks
easily (FRED in the query string, Telegram in the URL path), and HTTP errors print
the full URL, so redaction is applied to every log record as the very last step,
including records from third-party libraries.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any, TextIO

import structlog

from core import clock

REDACTED = "***"

# Field names whose value is always hidden, whatever it contains. The name has to
# end with the word, so "bot_token" and "X-Finnhub-Token" match but a usage counter
# such as "input_tokens" does not.
_SENSITIVE_NAME = re.compile(
    r"(?:^|[_\-.\s])"
    r"(?:secret(?:[_-]?key)?|token|password|passwd|api[_-]?key|authorization"
    r"|credentials?|user[_-]?agent)$",
    re.IGNORECASE,
)

# Shapes that are secrets wherever they appear in a message.
_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ...?api_key=VALUE, &token=VALUE, apikey=VALUE
    (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password)=([^&\s\"']+)"),
        rf"\1={REDACTED}",
    ),
    # Telegram: https://api.telegram.org/bot<id>:<token>/method
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), f"/bot{REDACTED}"),
    # Authorization: Bearer VALUE
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
)

# Shorter values are too likely to match ordinary text by accident.
_MIN_SECRET_LENGTH = 6

_SERVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class SecretRedactor:
    """structlog processor that removes secrets from an event before it is written."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        unique = {value for value in secret_values if len(value) >= _MIN_SECRET_LENGTH}
        # Longest first, so a secret that contains another is replaced whole.
        self._values = sorted(unique, key=len, reverse=True)

    def scrub_text(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern, replacement in _SENSITIVE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, Mapping):
            return {
                key: REDACTED if _SENSITIVE_NAME.search(str(key)) else self._scrub(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple | set | frozenset):
            return [self._scrub(item) for item in value]
        if isinstance(value, bytes):
            return self.scrub_text(value.decode("utf-8", errors="replace"))
        if value is None or isinstance(value, bool | int | float):
            return value
        # Anything else (exceptions, URLs, pydantic models...) is logged as text.
        return self.scrub_text(str(value))

    def __call__(self, logger: Any, method_name: str, event_dict: dict) -> dict:
        return {
            key: REDACTED if _SENSITIVE_NAME.search(str(key)) else self._scrub(value)
            for key, value in event_dict.items()
        }


def scrub_text(text: str, secret_values: Iterable[str] = ()) -> str:
    """Remove secrets from a text that will be stored or shown (error messages)."""
    return SecretRedactor(secret_values).scrub_text(text)


def _add_simulated_time(logger: Any, method_name: str, event_dict: dict) -> dict:
    """In replay, record the simulated time next to the real timestamp."""
    if clock.is_simulated():
        event_dict["sim_time"] = clock.now().isoformat()
    return event_dict


def configure_logging(
    level: str | int = "INFO",
    *,
    secret_values: Iterable[str] = (),
    stream: TextIO | None = None,
) -> None:
    """Route structlog and standard-library logging to one redacted JSON stream."""
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_simulated_time,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            SecretRedactor(secret_values),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # uvicorn installs its own plain-text handlers; send its records through ours so
    # access and error logs are JSON and redacted like everything else.
    for name in _SERVER_LOGGERS:
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    # Alembic reports two INFO lines every time a connection is inspected.
    logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
