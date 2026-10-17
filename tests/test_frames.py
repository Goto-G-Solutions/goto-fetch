"""to_frame and latest_per_key, run once for each installed dataframe library."""

import math
from typing import Any

import narwhals.stable.v2 as nw

from api_explorer import latest_per_key, parse_datetime, to_frame


def rows_of(frame: Any) -> list[dict]:
    """Read any frame back as plain rows, with NaN (pandas' null) as None."""

    def plain(value: Any) -> Any:
        missing = isinstance(value, float) and math.isnan(value)
        return None if missing else value

    rows = nw.from_native(frame, eager_only=True).rows(named=True)
    return [{k: plain(v) for k, v in row.items()} for row in rows]


def test_to_frame_has_every_column_seen_with_nulls_where_missing(backend):
    records = [{"id": 1, "name": "Ann"}, {"id": 2, "age": 30}]
    assert rows_of(to_frame(records, backend)) == [
        {"id": 1, "name": "Ann", "age": None},
        {"id": 2, "name": None, "age": 30},
    ]


def test_to_frame_of_no_records_is_an_empty_frame(backend):
    assert rows_of(to_frame([], backend)) == []


def test_to_frame_finds_a_type_that_first_appears_late(backend):
    # Regression: polars inferred types from the first 100 rows only.
    records = [{"n": None}] * 150 + [{"n": 7}]
    assert rows_of(to_frame(records, backend))[-1] == {"n": 7}


def test_to_frame_stores_a_column_of_mixed_types_as_text(backend, caplog):
    # Regression for the message: it called the ints "float".
    records = [{"value": "123"}, {"value": 456}, {"value": None}]
    assert [r["value"] for r in rows_of(to_frame(records, backend))] == [
        "123",
        "456",
        None,
    ]
    assert "Column 'value' mixes number, str; storing it as text" in caplog.messages


def test_to_frame_stores_list_elements_of_mixed_types_as_text(backend):
    records = [{"v": [1, 2]}, {"v": ["three"]}, {"v": []}]
    values = [list(r["v"]) for r in rows_of(to_frame(records, backend))]
    assert values == [["1", "2"], ["three"], []]


def test_latest_per_key_keeps_the_newest_row_of_each_key(backend):
    records = [
        {"id": 1, "seen": parse_datetime("2024-01-02T10:00:00Z"), "v": "old"},
        {"id": 1, "seen": parse_datetime("2024-03-01T09:00:00Z"), "v": "new"},
        {"id": 2, "seen": None, "v": "undated"},
        {"id": 2, "seen": parse_datetime("2024-02-01T00:00:00Z"), "v": "dated"},
    ]
    frame = to_frame(records, backend)
    kept = latest_per_key(frame, "id", "seen")
    # Narwhals hands back the library it was given.
    assert type(kept) is type(frame)
    assert sorted((r["id"], r["v"]) for r in rows_of(kept)) == [
        (1, "new"),
        (2, "dated"),
    ]


def test_latest_per_key_can_keep_the_earliest_instead(backend):
    frame = to_frame(
        [{"id": 1, "n": 2, "v": "b"}, {"id": 1, "n": 1, "v": "a"}], backend
    )
    assert [r["v"] for r in rows_of(latest_per_key(frame, "id", "n", "earliest"))] == [
        "a"
    ]
