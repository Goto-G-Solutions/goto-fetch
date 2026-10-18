"""SqliteWarehouse: what only the SQLite warehouse does, seen in SQLite itself.

The Warehouse promises are checked for every warehouse in
test_warehouses; this file looks inside the SQLite file: declared types,
the type-affinity trap, rebuilding a table to widen a column, and the
index that keeps replacing rows by key fast.
"""

import sqlite3
import sys

import pytest

from api_explorer import SqliteWarehouse
from api_explorer.testing import check_state_store


@pytest.fixture
def shop(tmp_path) -> SqliteWarehouse:
    return SqliteWarehouse(tmp_path / "shop.db")


def raw(shop: SqliteWarehouse, sql: str) -> list[tuple]:
    with sqlite3.connect(shop.path) as connection:
        return connection.execute(sql).fetchall()


def declared(shop: SqliteWarehouse, table: str) -> dict[str, str]:
    return {
        name: kind for _, name, kind, *_ in raw(shop, f"PRAGMA table_info({table})")
    }


def test_each_kind_of_value_gets_a_declared_type(shop):
    shop.write(
        "orders", [{"id": 1, "total": 9.5, "paid": True, "note": "a", "meta": {}}]
    )
    assert declared(shop, "orders") == {
        "id": "INTEGER",
        "total": "REAL",
        "paid": "BOOLEAN",
        "note": "TEXT",
        "meta": "JSON",
    }


def test_booleans_and_json_are_stored_as_sqlite_keeps_them_and_read_back_whole(shop):
    record = {"id": 1, "paid": True, "meta": {"a": [1, "b"]}}
    shop.write("orders", [record])
    assert raw(shop, "SELECT paid, meta FROM orders") == [(1, '{"a":[1,"b"]}')]
    assert shop.read("orders") == [record]


def test_text_that_looks_like_a_number_stays_text(shop):
    # The affinity trap: an INTEGER column would turn "5" into 5. A column
    # holding numbers and text is widened to TEXT first, so "5" stays "5"
    # and 5 becomes the JSON text "5", as in DuckDBWarehouse.
    shop.write("t", [{"id": 1, "v": 5}])
    shop.write("t", [{"id": 2, "v": "5"}, {"id": 3, "v": "five"}])
    assert raw(shop, "SELECT v, typeof(v) FROM t ORDER BY id") == [
        ("5", "text"),
        ("5", "text"),
        ("five", "text"),
    ]


@pytest.mark.parametrize(
    ("first", "then", "kind", "stored"),
    [
        (1, 2.5, "REAL", [1.0, 2.5]),
        (1, "lots", "TEXT", ["1", "lots"]),
        (True, "maybe", "TEXT", ["true", "maybe"]),
        ({"a": 1}, "plain", "TEXT", ['{"a":1}', "plain"]),
    ],
    ids=["int-then-float", "int-then-text", "bool-then-text", "json-then-text"],
)
def test_a_column_whose_values_change_kind_is_rebuilt_with_the_same_text_as_duckdb(
    shop, first, then, kind, stored
):
    shop.write("t", [{"id": 1, "v": first}], key="id")
    shop.write("t", [{"id": 2, "v": then}], key="id")
    assert declared(shop, "t")["v"] == kind
    assert [v for (v,) in raw(shop, "SELECT v FROM t ORDER BY id")] == stored


def test_replacing_by_key_uses_an_index_that_survives_a_rebuild(shop):
    shop.write("t", [{"id": 1, "v": 1}], key="id")
    shop.write("t", [{"id": 2, "v": "x"}], key="id")  # widening rebuilds the table
    indexes = raw(shop, "SELECT name FROM sqlite_master WHERE type = 'index'")
    assert ("t__id",) in indexes
    plan = raw(shop, "EXPLAIN QUERY PLAN DELETE FROM t WHERE id = 1")
    assert any("USING INDEX t__id" in str(step) for step in plan)


def test_a_sqlite_warehouse_keeps_state_in_the_same_file(shop):
    check_state_store(lambda: shop)
    tables = raw(shop, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert ("api_explorer_state",) in tables


def test_a_sqlite_warehouse_needs_a_file():
    with pytest.raises(ValueError, match="needs a file"):
        SqliteWarehouse(":memory:")


def test_without_sqlite3_the_error_says_what_is_missing(shop, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    with pytest.raises(ImportError, match="built without"):
        shop.write("t", [{"id": 1}])


def test_an_in_memory_sqlite_warehouse_points_to_the_memory_warehouse():
    # Regression: the message named SqliteState and MemoryState.
    with pytest.raises(
        ValueError, match="SqliteWarehouse needs a file.*MemoryWarehouse"
    ):
        SqliteWarehouse(":memory:")
