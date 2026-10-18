"""Warehouse: the promises every warehouse keeps, and the rules the database ones share.

check_warehouse states the promises once and runs them against each
warehouse here; its own tests show it catches each kind of broken one.
The rest covers what the DuckDB and SQLite warehouses share beyond the
promises (staging a run in batches, names that differ only in case,
refusing bad arguments), through read(), so the same test runs on both.
"""

import pytest

from api_explorer import (
    ApiError,
    Changes,
    DuckDBWarehouse,
    SqliteWarehouse,
    Warehouse,
)
from api_explorer.testing import MemoryWarehouse, check_warehouse


@pytest.fixture(params=["duckdb", "sqlite"])
def database(request, tmp_path):
    """A database warehouse in a fresh file: DuckDB (if installed) and SQLite."""

    def make() -> Warehouse:
        path = tmp_path / f"shop-{len(list(tmp_path.iterdir()))}"
        if request.param == "duckdb":
            pytest.importorskip("duckdb")
            return DuckDBWarehouse(path.with_suffix(".duckdb"))
        return SqliteWarehouse(path.with_suffix(".db"))

    return make


def test_the_database_warehouses_keep_every_warehouse_promise(database):
    check_warehouse(database)


def test_the_memory_warehouse_keeps_every_warehouse_promise():
    check_warehouse(MemoryWarehouse)


class Appends(MemoryWarehouse):
    """Broken: ignores the key and adds every record."""

    def write(self, table, records, *, key=None, deleted_by=None):
        return super().write(table, records, deleted_by=None)


class Partial(MemoryWarehouse):
    """Broken: stores records as they come, so a failure leaves some behind."""

    def write(self, table, records, *, key=None, deleted_by=None):
        count = 0
        for record in records:
            count += super().write(table, [record], key=key)
        return count


class KeepsDeleted(MemoryWarehouse):
    """Broken: never removes what deleted_by lists."""

    def write(self, table, records, *, key=None, deleted_by=None):
        return super().write(table, records, key=key)


class Rounds(MemoryWarehouse):
    """Broken: stores floats as whole numbers."""

    def write(self, table, records, *, key=None, deleted_by=None):
        rounded = [
            {k: round(v) if isinstance(v, float) else v for k, v in r.items()}
            for r in records
        ]
        return super().write(table, rounded, key=key, deleted_by=deleted_by)


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        (Appends, "with a key, a record must replace its row"),
        (Partial, "a write whose records failed must store nothing"),
        (KeepsDeleted, "deleted_by's keys must be removed"),
        (Rounds, "read must give back what was written"),
    ],
    ids=["ignores-key", "partial", "keeps-deleted", "rounds"],
)
def test_check_warehouse_catches_a_broken_warehouse(broken, message):
    with pytest.raises(AssertionError, match=message):
        check_warehouse(broken)


# What the database warehouses share beyond the promises


def failing_after(records):
    yield from records
    raise ApiError("the connection dropped")


def test_of_several_records_with_one_key_across_batches_the_last_wins(database):
    shop = database()
    batch = [{"id": 1, "total": 5}, {"id": 1, "total": 7}]
    assert shop.write("orders", batch, key="id", batch_size=1) == 2
    assert shop.read("orders") == [{"id": 1, "total": 7}]


def test_a_run_that_fails_after_several_batches_leaves_the_table_as_it_was(database):
    shop = database()
    shop.write("orders", [{"id": 1, "total": 5}], key="id")
    run = failing_after([{"id": 1, "total": 9}, {"id": 2, "total": 6, "new": "x"}])
    with pytest.raises(ApiError):
        shop.write("orders", run, key="id", batch_size=1)
    assert shop.read("orders") == [{"id": 1, "total": 5}]


def test_a_failure_while_storing_leaves_the_table_as_it_was(database, monkeypatch):
    # The failures above stop before the transaction; this one comes inside
    # it, after the columns changed and the first batch was stored.
    shop = database()
    shop.write("orders", [{"id": 1, "total": 5}], key="id")
    real_load = type(shop)._load
    loaded = []

    def load_then_fail(*args):
        if loaded:
            raise OSError("disk full")
        loaded.append(real_load(*args))

    monkeypatch.setattr(type(shop), "_load", staticmethod(load_then_fail))
    batch = [{"id": 1, "total": 9}, {"id": 2, "total": 6, "new": "x"}]
    with pytest.raises(OSError, match="disk full"):
        shop.write("orders", batch, key="id", batch_size=1)
    assert shop.read("orders") == [{"id": 1, "total": 5}]


def test_a_record_of_only_nulls_is_still_stored(database):
    # Regression: such a record once vanished, as no column could be typed.
    shop = database()
    assert shop.write("events", [{"refund": None}]) == 1
    assert shop.read("events") == [{"refund": None}]


@pytest.mark.parametrize("batch_size", [100, 1], ids=["one-batch", "across-batches"])
def test_fields_that_differ_only_in_case_are_refused(database, batch_size):
    with pytest.raises(ApiError, match="differ only in case"):
        database().write("t", [{"id": 1}, {"ID": 2}], batch_size=batch_size)


def test_a_field_lands_in_an_existing_column_whatever_its_case(database):
    shop = database()
    shop.write("t", [{"Name": "a"}])
    shop.write("t", [{"name": "b"}])
    assert sorted(r["Name"] for r in shop.read("t")) == ["a", "b"]


def test_read_takes_a_limit(database):
    shop = database()
    shop.write("t", [{"id": i} for i in range(5)])
    assert len(shop.read("t", limit=2)) == 2


def test_deleted_by_needs_a_key(database):
    with pytest.raises(ValueError, match="needs a key"):
        database().write("p", [], deleted_by=Changes("id"))


@pytest.mark.parametrize("batch_size", [0, -1])
def test_batch_size_must_be_positive(database, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        database().write("t", [], batch_size=batch_size)
