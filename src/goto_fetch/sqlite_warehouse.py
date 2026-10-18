"""A Warehouse in an SQLite file, using Python's own sqlite3: no extra package.

    shop = SqliteWarehouse("shop.db")
    with tracked_run(shop, "orders", Incremental, "updated_at",
                     "updated_since") as since:
        shop.write("orders", client.records("/orders/", incremental=since),
                   key="id")

Columns follow the records (see goto_fetch.warehouse), declared as
INTEGER, REAL, TEXT, BOOLEAN (stored as 1 and 0, read back as True and
False) and JSON (stored as text, read back parsed). SQLite can't change a
column's type, so widening one rebuilds the table, inside the same
transaction.

A trap SQLite sets, and how it is avoided: a column's declared type only
suggests one ("type affinity"), so a TEXT column turns 5 into "5", and an
INTEGER column turns the text "5" into 5. Values are therefore converted
to their column's kind before they are stored, and a column whose values
change kind is widened first, so SQLite never converts anything itself.
"""

import json
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .incremental import ReportsDeletions
from .state import SqliteState
from .warehouse import (
    Kind,
    check_table,
    matching,
    quote,
    read_part,
    to_kind,
    write_run,
)

if TYPE_CHECKING:
    # Imported when used: some Python builds leave sqlite3 out, and the
    # rest of the package must still import.
    import sqlite3

TYPES: dict[Kind, str] = {
    "boolean": "BOOLEAN",
    "integer": "INTEGER",
    "float": "REAL",
    "text": "TEXT",
    "json": "JSON",
}
KINDS: dict[str, Kind] = {name: kind for kind, name in TYPES.items()}

REBUILD_BATCH = 10_000
"""Rows copied at a time when a table is rebuilt, so memory doesn't grow with it."""


def _read_back(value: Any, kind: Kind) -> Any:
    """A stored value as read() gives it: booleans as bools, JSON parsed."""
    if value is None:
        return None
    if kind == "boolean":
        return bool(value)
    if kind == "json":
        return json.loads(value)
    return value


class SqliteWarehouse:
    """A Warehouse in an SQLite file, and a StateStore in the same file.

    Each call opens the file for one transaction and closes it, so other
    programs can read it between runs. Tracked runs' state lives in the
    table SqliteState uses.
    """

    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
        if str(path) == ":memory:" or str(path).startswith("file::memory:"):
            # Each call opens the file anew, so memory would be empty every
            # time.
            raise ValueError(
                "SqliteWarehouse needs a file; for one in memory, use "
                "goto_fetch.testing.MemoryWarehouse"
            )
        self._state = SqliteState(path, timeout=timeout)
        self._tables = _SqliteTables(self._state.path, timeout)
        self.path = self._state.path

    def __repr__(self) -> str:
        return f"SqliteWarehouse({str(self.path)!r})"

    def write(
        self,
        table: str,
        records: Iterable[Any],
        *,
        key: str | None = None,
        deleted_by: ReportsDeletions | None = None,
        batch_size: int = 10_000,
    ) -> int:
        """Store `records` in `table`, all or nothing; return how many were stored.

        See Warehouse for `key` and `deleted_by`, and write_run for the
        order of steps and `batch_size`, the records held in memory at a
        time.
        """
        return write_run(
            self._tables,
            table,
            records,
            key=key,
            deleted_by=deleted_by,
            batch_size=batch_size,
        )

    def read(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """`table`'s rows as dicts, at most `limit`; booleans and JSON come back whole."""
        check_table(table)
        with closing(self._tables.connect()) as connection:
            columns = self._tables.columns(connection, table)
            if not columns:
                return []
            query = f"SELECT * FROM {quote(table)}"
            cursor = connection.execute(
                query if limit is None else f"{query} LIMIT {int(limit)}"
            )
            names = [d[0] for d in cursor.description]
            found = cursor.fetchall()
        return [
            {
                n: _read_back(v, columns.get(n, "text"))
                for n, v in zip(names, row, strict=True)
            }
            for row in found
        ]

    def get(self, name: str) -> Any:
        """The state last saved under `name`, or None."""
        return self._state.get(name)

    def put(self, name: str, value: Any) -> None:
        """Save `value` under `name`, replacing what was there."""
        self._state.put(name, value)


class _SqliteTables:
    """SqlTables for an SQLite file: how write_run's steps are done in SQLite."""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout

    def connect(self) -> "sqlite3.Connection":
        try:
            import sqlite3
        except ImportError as exc:
            raise ImportError(
                "SqliteWarehouse needs Python's sqlite3 module, which this "
                "Python was built without; use DuckDBWarehouse"
            ) from exc
        # isolation_level=None leaves transactions to transaction(), so a
        # table rebuild and the rows that follow are one transaction.
        return sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)

    @contextmanager
    def transaction(self) -> Iterator["sqlite3.Connection"]:
        """Open the file for one transaction: commit on success, roll back on error."""
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def columns(self, connection: "sqlite3.Connection", table: str) -> dict[str, Kind]:
        """`table`'s columns and their kinds; a type this package doesn't make is text."""
        rows = connection.execute(
            "SELECT name, type FROM pragma_table_info(?) ORDER BY cid", (table,)
        ).fetchall()
        return {name: KINDS.get(str(kind).upper(), "text") for name, kind in rows}

    def alter(
        self,
        connection: "sqlite3.Connection",
        table: str,
        stored: dict[str, Kind],
        added: dict[str, Kind],
        changed: dict[str, Kind],
        after: dict[str, Kind],
    ) -> None:
        """Create, extend or (to change a kind) rebuild `table`."""
        if not stored:
            _create(connection, table, after)
        elif changed:
            _rebuild(connection, table, stored, after)
        else:
            for name, kind in added.items():
                connection.execute(
                    f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {TYPES[kind]}"
                )

    def load(
        self,
        connection: "sqlite3.Connection",
        table: str,
        part: Path,
        fields: list[str],
        columns: dict[str, Kind],
        key: str | None,
    ) -> None:
        """Store one staged file, replacing rows by `key`, each value in its column's kind."""
        rows = list(read_part(part))
        targets = [matching(columns, name) for name in fields]
        if key is not None:
            column = matching(columns, key)
            # An index, so replacing rows by key doesn't scan the table;
            # made here too, since a rebuild drops it.
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {quote(f'{table}__{column}')} "
                f"ON {quote(table)} ({quote(column)})"
            )
            self.delete(
                connection,
                table,
                column,
                [to_kind(row[key], columns[column]) for row in rows],
            )
        connection.executemany(
            f"INSERT INTO {quote(table)} ({', '.join(quote(t) for t in targets)}) "
            f"VALUES ({', '.join('?' for _ in targets)})",
            [
                tuple(
                    to_kind(row.get(name), columns[target])
                    for name, target in zip(fields, targets, strict=True)
                )
                for row in rows
            ],
        )

    def delete(
        self, connection: "sqlite3.Connection", table: str, column: str, keys: list[Any]
    ) -> None:
        """Remove the rows whose `column` holds one of `keys`."""
        connection.executemany(
            f"DELETE FROM {quote(table)} WHERE {quote(column)} = ?",
            [(key,) for key in keys],
        )


def _create(
    connection: "sqlite3.Connection", table: str, columns: dict[str, Kind]
) -> None:
    spec = ", ".join(f"{quote(n)} {TYPES[k]}" for n, k in columns.items())
    connection.execute(f"CREATE TABLE {quote(table)} ({spec})")


def _rebuild(
    connection: "sqlite3.Connection",
    table: str,
    before: dict[str, Kind],
    after: dict[str, Kind],
) -> None:
    """Remake `table` with the kinds in `after`, converting the values it holds.

    SQLite can't change a column's type in place. Each value is read back
    in its old kind and stored in its new one, as the Warehouse promise
    says (see goto_fetch.warehouse), REBUILD_BATCH rows at a time.
    """
    names = list(before)
    temporary = f"{table}__rebuilding"
    _create(connection, temporary, after)
    insert = (
        f"INSERT INTO {quote(temporary)} ({', '.join(quote(n) for n in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})"
    )
    old = connection.execute(
        f"SELECT {', '.join(quote(n) for n in names)} FROM {quote(table)}"
    )
    while batch := list(islice(old, REBUILD_BATCH)):
        # A second cursor for the inserts, so reading the old table goes on.
        connection.cursor().executemany(
            insert,
            [
                tuple(
                    to_kind(_read_back(value, before[name]), after[name])
                    for name, value in zip(names, row, strict=True)
                )
                for row in batch
            ],
        )
    connection.execute(f"DROP TABLE {quote(table)}")
    connection.execute(f"ALTER TABLE {quote(temporary)} RENAME TO {quote(table)}")
