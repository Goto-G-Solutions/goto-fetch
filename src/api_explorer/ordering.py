"""Ordering the values of a field that grows, such as updated_at, whatever form they take.

Incremental and Keyset both compare a field's values: numbers as numbers,
ISO timestamps as times (so "...Z" and "...+00:00" agree, and a time
without an offset counts as UTC), other text as text. Values that can't
be compared raise ApiError rather than guess.
"""

from datetime import UTC, datetime
from typing import Any

from .errors import ApiError
from .records import parse_datetime


def send_form(value: Any) -> Any:
    """The default form of a value sent to the API: as written, datetimes as ISO."""
    return value.isoformat() if isinstance(value, datetime) else value


def comparable(value: Any, field: str) -> Any:
    """A value of the cursor field in a form that compares with the others.

    Numbers compare as numbers. Text that reads as a timestamp compares as
    a time, so "...Z" and "...+00:00" agree; a timestamp without an offset
    counts as UTC. Other text compares as text.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, datetime)):
        raise ApiError(f"{field!r} holds {value!r}, which can't be ordered")
    if isinstance(value, str):
        value = parse_datetime(value) or value
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def greater(a: Any, b: Any, field: str) -> bool:
    try:
        return comparable(a, field) > comparable(b, field)
    except TypeError as exc:
        raise ApiError(
            f"{field!r} holds values that don't compare: {a!r} and {b!r}"
        ) from exc


def same(a: Any, b: Any, field: str) -> bool:
    return not greater(a, b, field) and not greater(b, a, field)
