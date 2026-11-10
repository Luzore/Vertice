"""Column types shared by all tables."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class UTCDateTime(TypeDecorator):
    """A UTC instant stored as fixed-width ISO-8601 text (principle 11).

    The fixed width makes text ordering equal to time ordering, so range queries
    work in SQLite. Naive datetimes are refused instead of being assumed to be UTC.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected a datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"naive datetime not allowed, attach a time zone: {value!r}")
        return value.astimezone(UTC).strftime(_UTC_FORMAT)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)


class JSONText(TypeDecorator):
    """A JSON document stored as text. Python ``None`` is stored as SQL NULL."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return json.loads(value)
