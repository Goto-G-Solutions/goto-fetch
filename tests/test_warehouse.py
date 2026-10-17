"""Warehouse: storing records in DuckDB, all or nothing, with the runs' state.

The promises: what write() stores and how columns follow the records,
replacing rows by key, removing rows Changes lists as deleted, leaving
the file as it was when anything fails, and keeping state as a
StateStore. The end-to-end tests run tracked runs into it.
"""

import dataclasses
import logging
import sys

import pytest

duckdb = pytest.importorskip("duckdb", reason="the warehouse needs the duckdb extra")

from api_explorer import ApiError, Changes, Incremental, logging_to, tracked_run
from api_explorer.testing import changes_api, check_state_store, flaky
from api_explorer.warehouse import Warehouse


@pytest.fixture
def shop(tmp_path) -> Warehouse:
    return Warehouse(tmp_path / "shop.duckdb")


def rows(shop: Warehouse, table: str, order: str = "1") -> list[tuple]:
    with duckdb.connect(str(shop.path)) as connection:
        return connection.sql(f"SELECT * FROM {table} ORDER BY {order}").fetchall()


def types(shop: Warehouse, table: str) -> dict[str, str]:
    with duckdb.connect(str(shop.path)) as connection:
        return {
            name: kind
            for name, kind, *_ in connection.sql(f"DESCRIBE {table}").fetchall()
        }


def failing_after(records):
    """Yield `records`, then fail, as a run whose last request fails does."""
    yield from records
    raise ApiError("the connection dropped")


# What write() stores


def test_write_stores_records_with_a_type_per_column(shop):
    count = shop.write("orders", [{"id": 1, "total": 9.5, "paid": True, "note": "a"}])
    assert count == 1
    assert rows(shop, "orders") == [(1, 9.5, True, "a")]
    assert types(shop, "orders") == {
        "id": "BIGINT",
        "total": "DOUBLE",
        "paid": "BOOLEAN",
        "note": "VARCHAR",
    }


def test_nested_values_are_stored_as_json_that_duckdb_can_query(shop):
    shop.write("orders", [{"id": 1, "customer": {"name": "Ann"}, "tags": ["new"]}])
    with duckdb.connect(str(shop.path)) as connection:
        found = connection.sql(
            "SELECT customer->>'name', tags->>0 FROM orders"
        ).fetchall()
    assert found == [("Ann", "new")]
    assert types(shop, "orders")["customer"] == "JSON"


def test_timestamps_stay_as_the_text_the_api_sent(shop):
    shop.write("orders", [{"id": 1, "updated_at": "2026-01-02T10:00:00+01:00"}])
    assert rows(shop, "orders") == [(1, "2026-01-02T10:00:00+01:00")]


def test_a_new_field_adds_a_column_and_older_rows_get_null(shop):
    shop.write("orders", [{"id": 1}])
    shop.write("orders", [{"id": 2, "coupon": "SPRING"}])
    assert rows(shop, "orders") == [(1, None), (2, "SPRING")]


def test_a_field_seen_only_as_null_takes_the_type_of_its_first_value(shop):
    shop.write("orders", [{"id": 1, "refund": None}])
    shop.write("orders", [{"id": 2, "refund": 3}])
    assert types(shop, "orders")["refund"] == "BIGINT"
    assert rows(shop, "orders") == [(1, None), (2, 3)]


def test_a_record_of_only_nulls_is_still_stored(shop):
    # Regression: such a record once vanished, as no column could be typed.
    assert shop.write("events", [{"refund": None}]) == 1
    assert rows(shop, "events") == [(None,)]


@pytest.mark.parametrize(
    ("first", "then", "kind", "stored"),
    [
        (1, 2.5, "DOUBLE", [1.0, 2.5]),
        (1, "lots", "VARCHAR", ["1", "lots"]),
        (True, "maybe", "VARCHAR", ["true", "maybe"]),
        ({"a": 1}, "plain", "VARCHAR", ['{"a":1}', "plain"]),
    ],
    ids=["int-then-float", "int-then-text", "bool-then-text", "json-then-text"],
)
def test_a_column_whose_values_change_type_is_widened_without_losing_any(
    shop, first, then, kind, stored
):
    shop.write("t", [{"id": 1, "v": first}])
    shop.write("t", [{"id": 2, "v": then}])
    assert types(shop, "t")["v"] == kind
    assert [v for _, v in rows(shop, "t")] == stored


def test_widening_a_column_is_logged_as_a_warning(shop):
    kept = []

    class Keep(logging.Handler):
        def emit(self, record):
            kept.append(record)

    shop.write("t", [{"id": 1, "v": 1}])
    with logging_to(Keep(), "INFO"):
        shop.write("t", [{"id": 2, "v": "x"}])
    events = {r.event: r.data for r in kept if hasattr(r, "event")}
    assert events["widen"] == {"table": "t", "column": "v", "type": "VARCHAR"}
    assert events["stored"] == {"table": "t", "records": 1}


def test_integers_beyond_64_bits_keep_every_digit(shop):
    shop.write("t", [{"id": 1, "v": 2**70}])
    assert rows(shop, "t") == [(1, str(2**70))]


def test_typed_records_are_stored_like_dicts(shop):
    @dataclasses.dataclass
    class Order:
        id: int
        total: float

    shop.write("orders", [Order(1, 2.5)])
    assert rows(shop, "orders") == [(1, 2.5)]


def test_odd_table_and_field_names_are_stored_as_given(shop):
    # Names reach SQL only quoted, so none of these can change the query.
    record = {"id": 1, 'it\'s "odd"': "a", "drop table x; --": "b", "with space": "c"}
    shop.write('my "orders"; --', [record])
    assert list(types(shop, '"my ""orders""; --"')) == list(record)


def test_a_column_emptied_during_a_run_is_widened_not_retyped(shop):
    # Regression: the run replaced the only row holding "" with a null, then
    # retyped the column to BIGINT for a later 0. DuckDB still held the old
    # row inside the transaction and failed to convert "". Columns now
    # change before any row does, judged on what the table held before.
    shop.write("t", [{"id": 0, "v": ""}], key="id")
    run = [{"id": 0, "v": None}, {"id": 1, "v": None}, {"id": 2, "v": 0}]
    shop.write("t", run, key="id", batch_size=2)
    assert types(shop, "t")["v"] == "VARCHAR"
    assert rows(shop, "t") == [(0, None), (1, None), (2, "0")]


def test_fields_that_differ_only_in_case_across_batches_are_refused(shop):
    with pytest.raises(ApiError, match="differ only in case"):
        shop.write("t", [{"id": 1}, {"ID": 2}], batch_size=1)


# Keys


def test_with_a_key_a_record_replaces_the_row_with_its_key(shop):
    shop.write("orders", [{"id": 1, "total": 5}, {"id": 2, "total": 6}], key="id")
    shop.write("orders", [{"id": 1, "total": 50}], key="id")
    assert rows(shop, "orders") == [(1, 50), (2, 6)]


def test_of_several_records_with_one_key_in_a_run_the_last_wins(shop):
    batch = [{"id": 1, "total": 5}, {"id": 1, "total": 7}]
    assert shop.write("orders", batch, key="id", batch_size=1) == 2
    assert rows(shop, "orders") == [(1, 7)]


def test_without_a_key_records_are_added(shop):
    shop.write("events", [{"id": 1}])
    shop.write("events", [{"id": 1}])
    assert rows(shop, "events") == [(1,), (1,)]


def test_a_record_without_its_key_is_refused_and_nothing_is_stored(shop):
    with pytest.raises(ApiError, match="no key 'id'"):
        shop.write("orders", [{"id": 1}, {"total": 3}], key="id")
    with duckdb.connect(str(shop.path)) as connection:
        assert connection.sql("SHOW TABLES").fetchall() == []


def test_fields_that_differ_only_in_case_are_refused(shop):
    with pytest.raises(ApiError, match="differ only in case"):
        shop.write("t", [{"id": 1, "ID": 2}])


def test_a_field_matches_an_existing_column_whatever_its_case(shop):
    shop.write("t", [{"Name": "a"}])
    shop.write("t", [{"name": "b"}])
    assert rows(shop, "t") == [("a",), ("b",)]


# All or nothing


def test_a_run_that_fails_part_way_leaves_the_table_as_it_was(shop):
    shop.write("orders", [{"id": 1, "total": 5}], key="id")
    with pytest.raises(ApiError):
        shop.write(
            "orders",
            failing_after([{"id": 1, "total": 9}, {"id": 2, "total": 6, "new": 1}]),
            key="id",
            batch_size=1,  # the failure comes after two batches were written
        )
    assert rows(shop, "orders") == [(1, 5)]
    assert "new" not in types(shop, "orders")


def test_a_first_run_that_fails_leaves_no_table(shop):
    with pytest.raises(ApiError):
        shop.write("orders", failing_after([{"id": 1}]), batch_size=1)
    with duckdb.connect(str(shop.path)) as connection:
        assert connection.sql("SHOW TABLES").fetchall() == []


def test_a_load_that_fails_part_way_leaves_the_table_as_it_was(shop, monkeypatch):
    # Fetch failures stop before the transaction (the test above); this one
    # fails inside it, after the columns changed and one file was stored.
    from api_explorer import warehouse

    shop.write("orders", [{"id": 1, "total": 5}], key="id")
    real_load = warehouse._load
    loaded = []

    def load_then_fail(*args):
        if loaded:
            raise OSError("disk full")
        loaded.append(real_load(*args))

    monkeypatch.setattr(warehouse, "_load", load_then_fail)
    batch = [{"id": 1, "total": 9}, {"id": 2, "total": 6, "new": "x"}]
    with pytest.raises(OSError, match="disk full"):
        shop.write("orders", batch, key="id", batch_size=1)
    assert rows(shop, "orders") == [(1, 5)]
    assert "new" not in types(shop, "orders")


# Deletions


def test_rows_a_changes_run_lists_as_deleted_are_removed(shop):
    first = Changes("id")
    shop.write(
        "p", first.track([{"id": 1}, {"id": 2}, {"id": 3}]), key="id", deleted_by=first
    )
    second = Changes("id", state=first.state)
    shop.write(
        "p", second.track([{"id": 1}, {"id": 3, "v": 1}]), key="id", deleted_by=second
    )
    assert rows(shop, "p") == [(1, None), (3, 1)]


def test_deleted_by_needs_a_key():
    with pytest.raises(ValueError, match="needs a key"):
        Warehouse("x.duckdb").write("p", [], deleted_by=Changes("id"))


def test_deleted_by_needs_a_finished_run(shop):
    with pytest.raises(ApiError, match="hasn't finished a run"):
        shop.write("p", [{"id": 1}], key="id", deleted_by=Changes("id"))
    with duckdb.connect(str(shop.path)) as connection:
        assert connection.sql("SHOW TABLES").fetchall() == []


# State


def test_a_warehouse_keeps_every_state_store_promise(shop):
    check_state_store(shop)


def test_a_warehouse_needs_a_file():
    with pytest.raises(ValueError, match="needs a file"):
        Warehouse(":memory:")


def test_without_duckdb_the_error_says_what_to_install(shop, monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", None)
    with pytest.raises(ImportError, match=r"api-explorer\[duckdb\]"):
        shop.write("t", [{"id": 1}])


@pytest.mark.parametrize("batch_size", [0, -1])
def test_batch_size_must_be_positive(shop, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        shop.write("t", [], batch_size=batch_size)


# End to end


def rec(id_: int, day: int) -> dict:
    return {"id": id_, "updated_at": f"2026-01-{day:02d}T00:00:00Z"}


def test_tracked_runs_into_a_warehouse_keep_the_table_and_the_state_in_step(shop):
    data = [rec(1, 1), rec(2, 2)]
    api = changes_api(data)

    def fetch_into(source):
        with tracked_run(
            shop, "orders", Incremental, "updated_at", "updated_since"
        ) as since:
            shop.write(
                "orders", source.client().records("/o/", incremental=since), key="id"
            )

    fetch_into(api)
    data.append(rec(3, 3))
    data[0] = rec(1, 4)
    with pytest.raises(ApiError):
        fetch_into(flaky(api, 503, 503, 503))  # retries run out
    assert rows(shop, "orders") == [
        (1, rec(1, 1)["updated_at"]),
        (2, rec(2, 2)["updated_at"]),
    ]
    assert shop.get("orders")["last"] == rec(2, 2)["updated_at"]
    fetch_into(api)
    assert rows(shop, "orders") == [
        tuple(r.values()) for r in sorted(data, key=lambda r: r["id"])
    ]
