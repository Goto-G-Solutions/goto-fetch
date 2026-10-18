"""DuckDBWarehouse: what only the DuckDB warehouse does, seen in DuckDB itself.

The Warehouse promises are checked for every warehouse in
test_warehouses; this file looks inside the DuckDB file instead: the
column types it makes, JSON that DuckDB can query, widening in place,
and the transaction order a regression taught.
"""

import dataclasses
import logging
import sys

import pytest

duckdb = pytest.importorskip("duckdb", reason="the warehouse needs the duckdb extra")

from api_explorer import DuckDBWarehouse, logging_to
from api_explorer.testing import check_state_store


@pytest.fixture
def shop(tmp_path) -> DuckDBWarehouse:
    return DuckDBWarehouse(tmp_path / "shop.duckdb")


def rows(shop: DuckDBWarehouse, table: str) -> list[tuple]:
    with duckdb.connect(str(shop.path)) as connection:
        return connection.sql(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def types(shop: DuckDBWarehouse, table: str) -> dict[str, str]:
    with duckdb.connect(str(shop.path)) as connection:
        described = connection.sql(f"DESCRIBE {table}").fetchall()
    return {name: kind for name, kind, *_ in described}


def test_each_kind_of_value_gets_a_duckdb_type(shop):
    shop.write(
        "orders", [{"id": 1, "total": 9.5, "paid": True, "note": "a", "meta": {}}]
    )
    assert types(shop, "orders") == {
        "id": "BIGINT",
        "total": "DOUBLE",
        "paid": "BOOLEAN",
        "note": "VARCHAR",
        "meta": "JSON",
    }


def test_nested_values_are_json_that_duckdb_can_query(shop):
    shop.write("orders", [{"id": 1, "customer": {"name": "Ann"}, "tags": ["new"]}])
    with duckdb.connect(str(shop.path)) as connection:
        found = connection.sql(
            "SELECT customer->>'name', tags->>0 FROM orders"
        ).fetchall()
    assert found == [("Ann", "new")]


def test_timestamps_stay_as_the_text_the_api_sent(shop):
    shop.write("orders", [{"id": 1, "updated_at": "2026-01-02T10:00:00+01:00"}])
    assert types(shop, "orders")["updated_at"] == "VARCHAR"


def test_a_field_seen_only_as_null_takes_the_type_of_its_first_value(shop):
    shop.write("orders", [{"id": 1, "refund": None}])
    assert types(shop, "orders")["refund"] == "VARCHAR"  # a place holder
    shop.write("orders", [{"id": 2, "refund": 3}])
    assert types(shop, "orders")["refund"] == "BIGINT"


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
def test_a_column_whose_values_change_kind_is_altered_in_place(
    shop, first, then, kind, stored
):
    shop.write("t", [{"id": 1, "v": first}])
    shop.write("t", [{"id": 2, "v": then}])
    assert types(shop, "t")["v"] == kind
    assert [v for _, v in rows(shop, "t")] == stored


def test_integers_beyond_64_bits_are_text_with_every_digit(shop):
    shop.write("t", [{"id": 1, "v": 2**70}])
    assert types(shop, "t")["v"] == "VARCHAR"
    assert rows(shop, "t") == [(1, str(2**70))]


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


def test_widening_a_column_is_logged_as_a_warning(shop):
    kept = []

    class Keep(logging.Handler):
        def emit(self, record):
            kept.append(record)

    shop.write("t", [{"id": 1, "v": 1}])
    with logging_to(Keep(), "INFO"):
        shop.write("t", [{"id": 2, "v": "x"}])
    events = {r.event: r.data for r in kept if hasattr(r, "event")}
    assert events["widen"] == {"table": "t", "column": "v", "type": "text"}
    assert events["stored"] == {"table": "t", "records": 1}


def test_typed_records_are_stored_like_dicts(shop):
    @dataclasses.dataclass
    class Order:
        id: int
        total: float

    shop.write("orders", [Order(1, 2.5)])
    assert rows(shop, "orders") == [(1, 2.5)]


def test_a_duckdb_warehouse_keeps_state_in_the_same_file(shop):
    check_state_store(lambda: shop)
    with duckdb.connect(str(shop.path)) as connection:
        assert ("api_explorer_state",) in connection.sql("SHOW TABLES").fetchall()


def test_a_duckdb_warehouse_needs_a_file():
    with pytest.raises(ValueError, match="needs a file"):
        DuckDBWarehouse(":memory:")


def test_without_duckdb_the_error_says_what_to_install(shop, monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", None)
    with pytest.raises(ImportError, match=r"api-explorer\[duckdb\]"):
        shop.write("t", [{"id": 1}])
