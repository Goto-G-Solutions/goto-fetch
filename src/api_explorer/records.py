"""Reshape lists of records (parsed JSON objects) without a dataframe library.

Each function takes records and returns new records, so they chain.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from .paths import get_path


def select(
    records: Iterable[dict],
    columns: Sequence[str],
    extractors: Mapping[str, Callable[[dict], Any]] | None = None,
) -> list[dict]:
    """Return one flat row per record, holding just `columns`.

    Each column is a path (see get_path), and the path is also the column's
    name in the row: select(rs, ["id", "user.name"]) gives rows like
    {"id": 1, "user.name": "Ann"}. When `extractors` has a function for a
    column, that function computes the value from the whole record instead.
    """
    extractors = extractors or {}
    return [
        {
            column: extractors[column](record)
            if column in extractors
            else get_path(record, column)
            for column in columns
        }
        for record in records
    ]


def flatten(record: dict, sep: str = ".", max_depth: int | None = None) -> dict:
    """Flatten one record into a single level of path-named keys.

    {"user": {"name": "Ann"}, "tags": ["a"]} gives
    {"user.name": "Ann", "tags[0]": "a"}. Below `max_depth` levels, values
    are kept whole. An empty dict or list is kept as a value, so its key
    doesn't vanish.

    A key holding the separator or a path character ([ ] | " ') goes in
    quotes, as get_path writes it: {"a.b": 1, "a": {"b": 2}} gives
    {'["a.b"]': 1, "a.b": 2}, so no two values collide, and with the
    default separator every key reads back with get_path. Empty keys and
    keys with spaces at either end are quoted too. A key named exactly "*"
    can't be read back, since paths always read "*" as the wildcard.
    """
    flat: dict = {}

    def segment(prefix: str, key: object) -> str:
        name = str(key)
        awkward = name == "" or name != name.strip()  # paths trim spaces
        if awkward or sep in name or any(c in name for c in "[]|\"'"):
            escaped = name.replace("\\", "\\\\").replace('"', '\\"')
            return f'{prefix}["{escaped}"]'
        return f"{prefix}{sep}{name}" if prefix else name

    def visit(value: Any, key: str, depth: int) -> None:
        deeper = max_depth is None or depth < max_depth
        if isinstance(value, dict) and value and deeper:
            for k, v in value.items():
                visit(v, segment(key, k), depth + 1)
        elif isinstance(value, list) and value and deeper:
            for i, v in enumerate(value):
                visit(v, f"{key}[{i}]", depth + 1)
        else:
            flat[key] = value

    for key, value in record.items():
        visit(value, segment("", key), 1)
    return flat


def explode(
    records: Iterable[dict],
    columns: Sequence[str] | None = None,
    fill: Literal["null", "forward"] = "null",
) -> list[dict]:
    """Return one row per list element, stepping through `columns` together.

    Row i of a record takes element i from each column. Within a record,
    shorter lists are padded to the longest: with None, or with fill="forward"
    with their last value. A record whose lists are all empty gives one row
    of None, so no record disappears. None counts as an empty list, and any
    other value as a one-element list. `columns` defaults to every key that
    holds a list in any record.
    """
    if fill not in ("null", "forward"):
        raise ValueError(f"fill must be 'null' or 'forward', got {fill!r}")
    records = list(records)
    if columns is None:
        columns = list(
            dict.fromkeys(
                key
                for record in records
                for key, value in record.items()
                if isinstance(value, list)
            )
        )

    def as_list(value: Any) -> list:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    rows = []
    for record in records:
        lists = {column: as_list(record.get(column)) for column in columns}
        length = max((len(values) for values in lists.values()), default=0)
        for i in range(max(length, 1)):
            row = dict(record)
            for column, values in lists.items():
                if i < len(values):
                    row[column] = values[i]
                elif fill == "forward" and values:
                    row[column] = values[-1]
                else:
                    row[column] = None
            rows.append(row)
    return rows


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Read an ISO-8601 timestamp, such as "2024-01-02T10:00:00Z".

    A datetime passes through unchanged. Anything unreadable gives None.
    A "Z" or an offset gives an aware datetime; no offset gives a naive one.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
