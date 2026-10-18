"""A Warehouse in a DuckDB file, which also keeps the state of tracked runs.

    shop = DuckDBWarehouse("shop.duckdb")
    with tracked_run(shop, "orders", Incremental, "updated_at",
                     "updated_since") as since:
        shop.write("orders", client.records("/orders/", incremental=since),
                   key="id")

Columns follow the records (see api_explorer.warehouse): nested objects
and lists are JSON columns, which DuckDB can query (meta->>'x'); text
stays text, timestamps included; integers are BIGINT and floats DOUBLE.

Needs the "duckdb" extra.
"""

import json
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .incremental import ReportsDeletions
from .warehouse import (
    FLOAT_EXACT,
    Kind,
    Staged,
    check_write,
    deleted_keys,
    keys_of_kind,
    log_stored,
    matching,
    plan_columns,
    quote,
    stage,
)

if TYPE_CHECKING:
    import duckdb

STATE_TABLE = "api_explorer_state"

TYPES: dict[Kind, str] = {
    "boolean": "BOOLEAN",
    "integer": "BIGINT",
    "float": "DOUBLE",
    "text": "VARCHAR",
    "json": "JSON",
}
KINDS: dict[str, Kind] = {name: kind for kind, name in TYPES.items()}


def _connect(path: Path) -> "duckdb.DuckDBPyConnection":
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(
            "DuckDBWarehouse needs DuckDB: install the extra, api-explorer[duckdb]"
        ) from exc
    return duckdb.connect(str(path))


def _sql_text(text: str) -> str:
    """`text` as a DuckDB string literal."""
    return "'" + text.replace("'", "''") + "'"


class DuckDBWarehouse:
    """A Warehouse in a DuckDB file, and a StateStore in the same file.

    Each call opens the file and closes it again, so other programs can
    read it between runs; DuckDB lets one process write to a file at a
    time. Tracked runs' state lives in a table of its own,
    api_explorer_state.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            # Each call opens the file anew, so memory would be empty
            # every time.
            raise ValueError("DuckDBWarehouse needs a file, not ':memory:'")
        self.path = Path(path)

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

        See Warehouse for `key` and `deleted_by`. `batch_size` is how many
        records are held in memory at a time. The run is read into
        temporary files first (about as much disk as its records as JSON),
        so a failed request never opens the database; then one transaction
        changes the columns and stores the records.
        """
        check_write(key, deleted_by, batch_size)
        with tempfile.TemporaryDirectory() as folder:
            run = stage(records, Path(folder), key, batch_size)
            gone = deleted_keys(deleted_by)
            with self._transaction() as connection:
                # Columns change before any row does: DuckDB keeps the old
                # versions of rows a transaction replaced, and retyping a
                # column would have to convert those too.
                columns = self._fit_columns(connection, table, run)
                for part in run.parts:
                    self._load(connection, table, part, run.kinds, columns, key)
                if gone and columns:
                    column = matching(columns, key or "")
                    if keys := keys_of_kind(gone, columns[column]):
                        connection.execute(
                            f"DELETE FROM {quote(table)} WHERE {quote(column)} "
                            "IN (SELECT unnest(?))",
                            [keys],
                        )
        log_stored(table, run.count)
        return run.count

    def read(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """`table`'s rows as dicts, at most `limit`; JSON columns come back parsed."""
        with closing(_connect(self.path)) as connection:
            columns = _columns(connection, table)
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
        with closing(_connect(self.path)) as connection:
            _make_state_table(connection)
            row = connection.execute(
                f"SELECT value FROM {STATE_TABLE} WHERE name = ?", [name]
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, name: str, value: Any) -> None:
        """Save `value` under `name`, replacing what was there."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"A state name must be non-empty text, got {name!r}")
        text = json.dumps(value, allow_nan=False)
        with self._transaction() as connection:
            _make_state_table(connection)
            connection.execute(
                f"INSERT OR REPLACE INTO {STATE_TABLE} VALUES (?, ?, ?)",
                [name, text, datetime.now(UTC).isoformat()],
            )

    @contextmanager
    def _transaction(self) -> Iterator["duckdb.DuckDBPyConnection"]:
        """Open the file for one transaction: commit on success, roll back on error."""
        with closing(_connect(self.path)) as connection:
            connection.begin()
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()

    @staticmethod
    def _fit_columns(
        connection: "duckdb.DuckDBPyConnection",
        table: str,
        run: Staged,
    ) -> dict[str, Kind]:
        """Make `table`'s columns take the run, and return every column's kind."""
        stored = _columns(connection, table)

        def only_nulls(column: str) -> bool:
            found = connection.execute(
                f"SELECT count({quote(column)}) FROM {quote(table)}"
            ).fetchone()
            return found is not None and found[0] == 0

        def holds_inexact(column: str) -> bool:
            found = connection.execute(
                f"SELECT count(*) FROM {quote(table)} "
                f"WHERE abs({quote(column)}) > {FLOAT_EXACT}"
            ).fetchone()
            return found is not None and found[0] > 0

        added, changed, after = plan_columns(
            stored, run, only_nulls, holds_inexact, table
        )
        if not stored:
            if after:
                columns = ", ".join(f"{quote(n)} {TYPES[k]}" for n, k in after.items())
                connection.execute(f"CREATE TABLE {quote(table)} ({columns})")
            return after
        for name, kind in added.items():
            connection.execute(
                f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {TYPES[kind]}"
            )
        for name, kind in changed.items():
            connection.execute(
                f"ALTER TABLE {quote(table)} ALTER {quote(name)} TYPE {TYPES[kind]}"
            )
        return after

    @staticmethod
    def _load(
        connection: "duckdb.DuckDBPyConnection",
        table: str,
        part: Path,
        incoming: dict[str, Kind | None],
        columns: dict[str, Kind],
        key: str | None,
    ) -> None:
        """Store one staged file in `table`, replacing rows by `key` if given."""
        spec = ", ".join(
            f"{_sql_text(name)}: {_sql_text(TYPES[columns[matching(columns, name)]])}"
            for name in incoming
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


def _columns(connection: "duckdb.DuckDBPyConnection", table: str) -> dict[str, Kind]:
    """`table`'s columns and their kinds, in order, or {} if there is no such table.

    A type this package doesn't make counts as text.
    """
    rows = connection.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? AND table_schema = current_schema() "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return {name: KINDS.get(kind, "text") for name, kind in rows}


def _make_state_table(connection: "duckdb.DuckDBPyConnection") -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
        "name VARCHAR PRIMARY KEY, value VARCHAR NOT NULL, saved_at VARCHAR NOT NULL)"
    )
