# Storage

A warehouse keeps fetched records in tables, and a state store keeps where each tracked run stopped. The built-in warehouses are both, in one file.

## Choosing

| You want... | Use |
|-------------|-----|
| fast analysis, SQL over JSON, polars and pandas straight from the file | `DuckDBWarehouse("shop.duckdb")` (the `duckdb` extra) |
| one portable file, nothing to install | `SqliteWarehouse("shop.db")` |
| only the state, records kept elsewhere | `SqliteState("state.db")` |
| another database | a `Warehouse` of your own, or `SqlTables` with `write_run` ([build-your-own](build-your-own.md)) |

## What it handles for you

Every warehouse keeps these promises (and `check_warehouse` checks them):

- **All or nothing:** one `write` either stores the whole run or leaves the table as it was, even if a request fails part way. The database warehouses copy the run to temporary files first, so a failed request never touches the database.
- **Keys:** with `key=`, a record replaces the row with its key, and of several with one key in a run, the last wins. Without one, records are added.
- **Deletions:** `deleted_by=` removes the keys a tracker lists as gone, only once the records are stored.
- **No value lost:** columns follow the records. Nested objects and lists are JSON columns, text stays text (timestamps included), new fields add columns, and a column whose values change type is widened: integers beside floats become floats when that loses nothing, anything else becomes text, with a warning. A record without its key, or with no fields at all, is refused before anything is written.

## Examples

```python
from goto_fetch import SqliteWarehouse

shop = SqliteWarehouse("shop.db")
shop.write("orders", [{"id": 1, "total": 9.5, "customer": {"name": "Ann"}}], key="id")
shop.write(
    "orders",
    [{"id": 1, "total": 12.0}, {"id": 2, "total": 3, "coupon": "SPRING"}],
    key="id",
)

rows = sorted(shop.read("orders"), key=lambda r: r["id"])
assert rows == [
    {"id": 1, "total": 12.0, "customer": None, "coupon": None},
    {"id": 2, "total": 3.0, "customer": None, "coupon": "SPRING"},
]
```

The file is plain SQLite (or DuckDB), so any tool reads it:

```python skip
import duckdb

df = (
    duckdb.connect("shop.duckdb")
    .sql("SELECT id, customer->>'name' AS name FROM orders")
    .pl()
)
```

### State between runs

`tracked_run(store, name, tracker class, ...)` builds the tracker from the saved state, and saves the new state when the block ends without an error. A warehouse is a state store too, so one file holds both:

```python skip
with tracked_run(shop, "orders", Incremental, "updated_at", "updated_since") as since:
    shop.write("orders", client.records("/orders/", incremental=since), key="id")
```

## Traps

- **`key` is a top-level field** of the records (`"id"`), not a path.
- **`goto_fetch_state` is taken:** the built-in warehouses keep tracked runs' state in a table of that name, in the same file, and refuse it for records.
- **Fields differing only in case** (`id` and `ID`) are refused: databases treat them as one column.
- **DuckDB lets one process write to a file at a time.** Each call opens and closes the file, so readers fit between runs.
- **Memory:** `write` holds `batch_size` records at a time (10,000 by default) while it copies the run to disk; lower it for very wide records.
- **Two runs under one name at once** are safe but wasteful: the last to finish wins, and at worst the next run fetches some records again.
- **Records and state commit apart,** even in one file: `write` commits, then `tracked_run` saves. A crash between the two repeats the run, which `key=` makes harmless; see the three guarantees in [incremental](incremental.md).
- Timestamps stay text, as the API sent them; cast in SQL (`CAST(updated_at AS TIMESTAMPTZ)` in DuckDB).
