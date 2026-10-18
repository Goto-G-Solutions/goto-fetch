"""A Warehouse in an SQLite file, using Python's own sqlite3: no extra package.

    shop = SqliteWarehouse("shop.db")
    with tracked_run(shop, "orders", Incremental, "updated_at",
                     "updated_since") as since:
        shop.write("orders", client.records("/orders/", incremental=since),
                   key="id")

Columns follow the records as in DuckDBWarehouse (see
api_explorer.warehouse), declared as INTEGER, REAL, TEXT, BOOLEAN (stored
as 1 and 0, read back as True and False) and JSON (stored as text, read
back parsed). SQLite can't change a column's type, so widening one
rebuilds the table, inside the same transaction.

A trap SQLite sets, and how it is avoided: a column's declared type only
suggests one ("type affinity"), so a TEXT column turns 5 into "5", and an
INTEGER column turns the text "5" into 5. Values are therefore converted
to their column's kind before they are stored, and a column whose values
change kind is widened first, so SQLite never converts anything itself.
"""

import json
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .incremental import ReportsDeletions
from .state import SqliteState
from .warehouse import (
    Kind,
    as_text,
    check_write,
    deleted_keys,
    log_stored,
    matching,
    plan_columns,
    quote,
    stage,
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


def _stored(value: Any, kind: Kind) -> Any:
    """`value` in the form SQLite keeps for a column of `kind`."""
    if value is None:
        return None
    if kind == "text":
        return as_text(value)
    if kind == "json":
        return json.dumps(value, separators=(",", ":"))
    if kind == "boolean":
        return int(value)
    if kind == "float":
        return float(value)
    return value


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
        self._state = SqliteState(path, timeout=timeout)
        self.path = self._state.path
        self.timeout = timeout

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

        See Warehouse for `key` and `deleted_by`, and DuckDBWarehouse.write
        for `batch_size` and the temporary files.
        """
        check_write(key, deleted_by, batch_size)
        with tempfile.TemporaryDirectory() as folder:
            run = stage(records, Path(folder), key, batch_size)
            gone = deleted_keys(deleted_by)
            with self._transaction() as connection:
                columns = self._fit_columns(connection, table, run.kinds)
                if key is not None and columns:
                    _index(connection, table, matching(columns, key))
                for part in run.parts:
                    self._load(connection, table, run.rows(part), columns, key)
                if gone and columns:
                    column = quote(matching(columns, key or ""))
                    connection.executemany(
                        f"DELETE FROM {quote(table)} WHERE {column} = ?",
                        [(k,) for k in gone],
                    )
        log_stored(table, run.count)
        return run.count

    def read(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """`table`'s rows as dicts, at most `limit`, as SqliteWarehouse stored them."""
        with closing(self._connect()) as connection:
            columns = _columns(connection, table)
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

    def _connect(self) -> "sqlite3.Connection":
        try:
            import sqlite3
        except ImportError as exc:
            raise ImportError(
                "SqliteWarehouse needs Python's sqlite3 module, which this "
                "Python was built without; use DuckDBWarehouse"
            ) from exc
        # isolation_level=None leaves transactions to _transaction, so a
        # table rebuild and the rows that follow are one transaction.
        return sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)

    @contextmanager
    def _transaction(self) -> Iterator["sqlite3.Connection"]:
        """Open the file for one transaction: commit on success, roll back on error."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    @staticmethod
    def _fit_columns(
        connection: "sqlite3.Connection",
        table: str,
        incoming: dict[str, Kind | None],
    ) -> dict[str, Kind]:
        """Make `table`'s columns take the run, and return every column's kind."""
        stored = _columns(connection, table)

        def only_nulls(column: str) -> bool:
            found = connection.execute(
                f"SELECT count({quote(column)}) FROM {quote(table)}"
            ).fetchone()
            return found is not None and found[0] == 0

        added, changed, after = plan_columns(stored, incoming, only_nulls, table)
        if not stored:
            if after:
                _create(connection, table, after)
            return after
        if changed:
            _rebuild(connection, table, stored, after)
            return after
        for name, kind in added.items():
            connection.execute(
                f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {TYPES[kind]}"
            )
        return after

    @staticmethod
    def _load(
        connection: "sqlite3.Connection",
        table: str,
        rows: Iterable[dict[str, Any]],
        columns: dict[str, Kind],
        key: str | None,
    ) -> None:
        """Store staged rows in `table`, replacing rows by `key` if given."""
        batch = list(rows)
        if not batch:
            return
        names = list(dict.fromkeys(n for row in batch for n in row))
        targets = [matching(columns, n) for n in names]
        if key is not None:
            column = quote(matching(columns, key))
            connection.executemany(
                f"DELETE FROM {quote(table)} WHERE {column} = ?",
                [
                    (_stored(row[key], columns[matching(columns, key)]),)
                    for row in batch
                ],
            )
        placeholders = ", ".join("?" for _ in names)
        connection.executemany(
            f"INSERT INTO {quote(table)} ({', '.join(quote(t) for t in targets)}) "
            f"VALUES ({placeholders})",
            [
                tuple(
                    _stored(row.get(n), columns[t])
                    for n, t in zip(names, targets, strict=True)
                )
                for row in batch
            ],
        )


def _columns(connection: "sqlite3.Connection", table: str) -> dict[str, Kind]:
    """`table`'s columns and their kinds, in order, or {} if there is no such table.

    A declared type this package doesn't make counts as text.
    """
    rows = connection.execute(
        "SELECT name, type FROM pragma_table_info(?) ORDER BY cid", (table,)
    ).fetchall()
    return {name: KINDS.get(str(kind).upper(), "text") for name, kind in rows}


def _create(
    connection: "sqlite3.Connection", table: str, columns: dict[str, Kind]
) -> None:
    spec = ", ".join(f"{quote(n)} {TYPES[k]}" for n, k in columns.items())
    connection.execute(f"CREATE TABLE {quote(table)} ({spec})")


def _index(connection: "sqlite3.Connection", table: str, column: str) -> None:
    """Index `column`, so replacing rows by key doesn't scan the whole table."""
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS {quote(f'{table}__{column}')} "
        f"ON {quote(table)} ({quote(column)})"
    )


def _rebuild(
    connection: "sqlite3.Connection",
    table: str,
    before: dict[str, Kind],
    after: dict[str, Kind],
) -> None:
    """Remake `table` with the kinds in `after`, converting the values it holds.

    SQLite can't change a column's type in place. Each value is read back
    in its old kind and stored in its new one, so a widened column holds
    the same JSON text DuckDBWarehouse gives ("1", "true", '{"a":1}').
    Indexes are made again by write().
    """
    names = list(before)
    old = connection.execute(
        f"SELECT {', '.join(quote(n) for n in names)} FROM {quote(table)}"
    ).fetchall()
    temporary = f"{table}__rebuilding"
    _create(connection, temporary, after)
    connection.executemany(
        f"INSERT INTO {quote(temporary)} ({', '.join(quote(n) for n in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        [
            tuple(
                _stored(_read_back(value, before[name]), after[name])
                for name, value in zip(names, row, strict=True)
            )
            for row in old
        ],
    )
    connection.execute(f"DROP TABLE {quote(table)}")
    connection.execute(f"ALTER TABLE {quote(temporary)} RENAME TO {quote(table)}")
