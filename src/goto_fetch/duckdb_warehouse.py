"""A Warehouse in a DuckDB file, which also keeps the state of tracked runs.

    shop = DuckDBWarehouse("shop.duckdb")
    with tracked_run(shop, "orders", Incremental, "updated_at",
                     "updated_since") as since:
        shop.write("orders", client.records("/orders/", incremental=since),
                   key="id")

Columns follow the records (see goto_fetch.warehouse): nested objects
and lists are JSON columns, which DuckDB can query (meta->>'x'); text
stays text, timestamps included; integers are BIGINT and floats DOUBLE.

Needs the "duckdb" extra.
"""

import json
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .incremental import ReportsDeletions
from .state import STATE_TABLE, check_state_name
from .warehouse import Kind, check_limit, check_table, matching, quote, write_run

if TYPE_CHECKING:
    import duckdb

TYPES: dict[Kind, str] = {
    "boolean": "BOOLEAN",
    "integer": "BIGINT",
    "float": "DOUBLE",
    "text": "VARCHAR",
    "json": "JSON",
}
KINDS: dict[str, Kind] = {name: kind for kind, name in TYPES.items()}


def _sql_text(text: str) -> str:
    """`text` as a DuckDB string literal."""
    return "'" + text.replace("'", "''") + "'"


class DuckDBWarehouse:
    """A Warehouse in a DuckDB file, and a StateStore in the same file.

    Each call opens the file and closes it again, so other programs can
    read it between runs; DuckDB lets one process write to a file at a
    time. Tracked runs' state lives in a table of its own,
    goto_fetch_state.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            # Each call opens the file anew, so memory would be empty
            # every time.
            raise ValueError(
                "DuckDBWarehouse needs a file; for one in memory, use "
                "goto_fetch.testing.MemoryWarehouse"
            )
        self.path = Path(path)
        self._tables = _DuckDBTables(self.path)

    def __repr__(self) -> str:
        return f"DuckDBWarehouse({str(self.path)!r})"

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
        """`table`'s rows as dicts, at most `limit`; JSON columns come back parsed."""
        check_limit(limit)
        check_table(table)
        with closing(self._tables.connect()) as connection:
            columns = self._tables.columns(connection, table)
            if not columns:
                return []
            query = f"SELECT * FROM {quote(table)}"
            result = connection.execute(
                query if limit is None else f"{query} LIMIT {int(limit)}"
            )
            names = [d[0] for d in result.description or []]
            found = result.fetchall()
        parsed = [name for name in names if columns.get(name) == "json"]
        rows = [dict(zip(names, row, strict=True)) for row in found]
        for row in rows:
            for name in parsed:
                if isinstance(row[name], str):
                    row[name] = json.loads(row[name])
        return rows

    def get(self, name: str) -> Any:
        """The state last saved under `name`, or None."""
        check_state_name(name)
        with closing(self._tables.connect()) as connection:
            _make_state_table(connection)
            row = connection.execute(
                f"SELECT value FROM {STATE_TABLE} WHERE name = ?", [name]
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, name: str, value: Any) -> None:
        """Save `value` under `name`, replacing what was there."""
        check_state_name(name)
        text = json.dumps(value, allow_nan=False)
        with self._tables.transaction() as connection:
            _make_state_table(connection)
            connection.execute(
                f"INSERT OR REPLACE INTO {STATE_TABLE} VALUES (?, ?, ?)",
                [name, text, datetime.now(UTC).isoformat()],
            )


class _DuckDBTables:
    """SqlTables for a DuckDB file: how write_run's steps are done in DuckDB."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> "duckdb.DuckDBPyConnection":
        try:
            import duckdb
        except ImportError as exc:
            raise ImportError(
                "DuckDBWarehouse needs DuckDB: install the extra, goto-fetch[duckdb]"
            ) from exc
        return duckdb.connect(str(self.path))

    @contextmanager
    def transaction(self) -> Iterator["duckdb.DuckDBPyConnection"]:
        """Open the file for one transaction: commit on success, roll back on error."""
        with closing(self.connect()) as connection:
            connection.begin()
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()

    def columns(
        self, connection: "duckdb.DuckDBPyConnection", table: str
    ) -> dict[str, Kind]:
        """`table`'s columns and their kinds; a type this package doesn't make is text."""
        rows = connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = current_schema() "
            "ORDER BY ordinal_position",
            [table],
        ).fetchall()
        return {name: KINDS.get(kind, "text") for name, kind in rows}

    def alter(
        self,
        connection: "duckdb.DuckDBPyConnection",
        table: str,
        stored: dict[str, Kind],
        added: dict[str, Kind],
        changed: dict[str, Kind],
        after: dict[str, Kind],
    ) -> None:
        """Create `table`, or add and retype its columns in place."""
        if not stored:
            columns = ", ".join(f"{quote(n)} {TYPES[k]}" for n, k in after.items())
            connection.execute(f"CREATE TABLE {quote(table)} ({columns})")
            return
        for name, kind in added.items():
            connection.execute(
                f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {TYPES[kind]}"
            )
        for name, kind in changed.items():
            connection.execute(
                f"ALTER TABLE {quote(table)} ALTER {quote(name)} TYPE {TYPES[kind]}"
            )

    def load(
        self,
        connection: "duckdb.DuckDBPyConnection",
        table: str,
        part: Path,
        fields: list[str],
        columns: dict[str, Kind],
        key: str | None,
    ) -> None:
        """Store one staged file, read by DuckDB itself with each column's type."""
        spec = ", ".join(
            f"{_sql_text(name)}: {_sql_text(TYPES[columns[matching(columns, name)]])}"
            for name in fields
        )
        source = (
            f"read_json({_sql_text(str(part))}, format='newline_delimited', "
            f"columns={{{spec}}})"
        )
        if key is not None:
            connection.execute(
                f"DELETE FROM {quote(table)} WHERE {quote(matching(columns, key))} "
                f"IN (SELECT {quote(key)} FROM {source})"
            )
        connection.execute(f"INSERT INTO {quote(table)} BY NAME SELECT * FROM {source}")

    def delete(
        self,
        connection: "duckdb.DuckDBPyConnection",
        table: str,
        column: str,
        keys: list[Any],
    ) -> None:
        """Remove the rows whose `column` holds one of `keys`."""
        connection.execute(
            f"DELETE FROM {quote(table)} WHERE {quote(column)} IN (SELECT unnest(?))",
            [keys],
        )


def _make_state_table(connection: "duckdb.DuckDBPyConnection") -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
        "name VARCHAR PRIMARY KEY, value VARCHAR NOT NULL, saved_at VARCHAR NOT NULL)"
    )
