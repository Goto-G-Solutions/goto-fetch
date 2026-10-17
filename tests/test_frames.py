"""to_frame and latest_per_key, run once for each installed dataframe library."""

import math
import sys
from typing import Any

import narwhals.stable.v2 as nw
import pytest

from api_explorer import (
    default_backend,
    frames,
    latest_per_key,
    parse_datetime,
    to_frame,
)


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


def test_to_frame_stores_list_elements_of_mixed_types_as_text(backend, caplog):
    records = [{"v": [1, 2]}, {"v": ["three"]}, {"v": []}]
    values = [list(r["v"]) for r in rows_of(to_frame(records, backend))]
    assert values == [["1", "2"], ["three"], []]
    assert "Column 'v[*]' mixes number, str; storing it as text" in caplog.messages


def test_to_frame_evens_out_the_fields_of_nested_objects(backend, caplog):
    # Regression: types were evened out for top-level columns only, so
    # polars and pyarrow failed on a nested field holding 1 here and "x" there.
    records = [
        {"meta": {"size": 1, "ok": True}},
        {"meta": {"size": "x"}},
        {"meta": None},
    ]
    frame = nw.from_native(to_frame(records, backend), eager_only=True)
    sizes = frame["meta"].to_list()
    assert [None if m is None else m["size"] for m in sizes] == ["1", "x", None]
    assert "Column 'meta.size' mixes number, str; storing it as text" in caplog.messages


def test_to_frame_turns_ints_beside_floats_into_floats(backend):
    # Regression, found by the property tests: polars refused [0, 0.0]
    # inside a list.
    records = [{"v": [None, 0, 0.5]}, {"n": 1}, {"n": 2.5}]
    rows = rows_of(to_frame(records, backend))
    assert list(rows[0]["v"]) == [None, 0.0, 0.5]
    assert [r["n"] for r in rows[1:]] == [1.0, 2.5]


def test_to_frame_stores_integers_beyond_64_bits_as_text(backend, caplog):
    # Regression: pyarrow raised OverflowError on these.
    big = 2**70
    rows = rows_of(to_frame([{"id": big}, {"id": 1}], backend))
    assert [r["id"] for r in rows] == [str(big), "1"]
    assert (
        "Column 'id' holds integers too large for 64 bits; storing it as text"
        in caplog.messages
    )


def test_to_frame_evens_out_objects_inside_lists(backend):
    # The same rule reaches objects nested in lists, at any depth.
    records = [{"lines": [{"qty": 1}, {"qty": 2}]}, {"lines": [{"qty": "lots"}]}]
    rows = nw.from_native(to_frame(records, backend), eager_only=True)[
        "lines"
    ].to_list()
    assert [[line["qty"] for line in lines] for lines in rows] == [["1", "2"], ["lots"]]


def test_to_frame_takes_a_list_whose_first_inner_values_are_all_null(backend):
    # Regression: polars typed the column by its first value, list[list[null]],
    # then failed with an internal panic on [[True]]. Hypothesis found it.
    records = [{"a": [[None]]}, {"a": [[True]]}]
    column = nw.from_native(to_frame(records, backend), eager_only=True)["a"]
    assert [list(map(list, v)) for v in column.to_list()] == [[[None]], [[True]]]


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


@pytest.fixture
def no_frame_libraries(monkeypatch):
    """A session where no dataframe library is imported or installed, until a test says so."""
    for name in frames.BACKENDS:
        monkeypatch.delitem(sys.modules, name, raising=False)
    installed: set[str] = set()
    monkeypatch.setattr(
        frames, "find_spec", lambda name: object() if name in installed else None
    )
    return installed


def test_default_backend_prefers_a_library_already_imported(
    no_frame_libraries, monkeypatch
):
    no_frame_libraries.update({"polars", "pandas"})
    monkeypatch.setitem(sys.modules, "pandas", object())
    assert default_backend() == "pandas"


def test_default_backend_falls_back_to_the_first_installed(no_frame_libraries):
    no_frame_libraries.update({"pandas", "pyarrow"})
    assert default_backend() == "pandas"


def test_default_backend_says_what_to_install_when_there_is_none(no_frame_libraries):
    with pytest.raises(ImportError, match="install one of: polars, pandas, pyarrow"):
        default_backend()


def test_to_frame_without_a_backend_uses_the_default(backend):
    # The other tests pass the backend; this one checks None means default_backend().
    assert type(to_frame([{"id": 1}])) is type(to_frame([{"id": 1}], default_backend()))
