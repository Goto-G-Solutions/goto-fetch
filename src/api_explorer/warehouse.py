"""Store records in a DuckDB file, with the state of the runs that fetched them.

A Warehouse is one DuckDB file. write() puts a run's records in a table,
all or nothing, replacing rows by key when there is one; the same file
keeps tracked runs' state, so it is also a StateStore:

    shop = Warehouse("shop.duckdb")
    with tracked_run(shop, "orders", Incremental, "updated_at",
                     "updated_since") as since:
        shop.write("orders", client.records("/orders/", incremental=since),
                   key="id")

Columns follow the records. Scalars get a type of their own; nested
objects and lists are stored as JSON, which DuckDB can still query
(meta->>'x'); text stays text, timestamps included, so nothing is
reinterpreted. A field seen for the first time adds a column; one seen
only as null so far takes the type of its first value. A column whose
values change type is widened: integers beside floats become DOUBLE,
anything else becomes text, with a warning, so no value is lost.

Needs the "duckdb" extra.
"""

import json
import logging
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ApiError
from .incremental import Changes
from .logs import log_event
from .models import as_dict

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger(__name__)

INT64 = range(-(2**63), 2**63)
STATE_TABLE = "api_explorer_state"


def _quote(name: str) -> str:
    """`name` as a DuckDB identifier, safe whatever it holds."""
    return '"' + name.replace('"', '""') + '"'


def _type_of(values: Iterable[Any]) -> str | None:
    """The DuckDB type that holds every one of `values`, or None if all are null.

    Mixed kinds give VARCHAR, except integers beside floats, which give
    DOUBLE. Integers beyond 64 bits give VARCHAR, so no digit is lost.
    """
    kinds = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add("BOOLEAN")
        elif isinstance(value, int):
            kinds.add("BIGINT" if value in INT64 else "VARCHAR")
        elif isinstance(value, float):
            kinds.add("DOUBLE")
        elif isinstance(value, (dict, list)):
            kinds.add("JSON")
        else:
            kinds.add("VARCHAR")
    if not kinds:
        return None
    if len(kinds) == 1:
        return kinds.pop()
    return "DOUBLE" if kinds == {"BIGINT", "DOUBLE"} else "VARCHAR"


def _widen(stored: str, incoming: str) -> str:
    """The type a column needs to keep its `stored` values and take `incoming` ones."""
    if stored == incoming:
        return stored
    if {stored, incoming} == {"BIGINT", "DOUBLE"}:
        return "DOUBLE"
    return "VARCHAR"


def _connect(path: Path) -> "duckdb.DuckDBPyConnection":
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(
            "Warehouse needs DuckDB: install the extra, api-explorer[duckdb]"
        ) from exc
    return duckdb.connect(str(path))


class Warehouse:
    """A DuckDB file that holds fetched records and the state of tracked runs.

    write() stores records; get() and put() make it a StateStore, in a
    table of its own (api_explorer_state), so one file holds the data and
    where each run stopped. Each call opens the file and closes it again,
    so other programs can read it between runs. DuckDB lets one process
    write to a file at a time.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            # Each call opens the file anew, so memory would be empty
            # every time.
            raise ValueError("Warehouse needs a file, not ':memory:'")
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"Warehouse({str(self.path)!r})"

    def write(
        self,
        table: str,
        records: Iterable[Any],
        *,
        key: str | None = None,
        deleted_by: Changes | None = None,
        batch_size: int = 10_000,
    ) -> int:
        """Store `records` in `table`, all or nothing, and return how many were written.

        - `key`: a top-level field that identifies a record, such as "id".
          A record whose key is already in the table replaces that row,
          and of several with one key in a run, the last wins. Without a
          key, records are added.
        - `deleted_by`: a Changes whose run gave `records`; once they are
          all stored, the rows of the keys it lists as deleted are removed.
          Needs `key`.
        - `batch_size`: records held in memory at a time.

        It works in two steps. First it reads every record, writing them
        to temporary files and working out each column's type; a failure
        here (a failed request, say) never touches the file. Then one
        transaction changes the table's columns and stores the records. If
        anything fails, the table is left as it was, which is what
        Changes.deleted relies on. The temporary files need about as much
        disk as the run's records as JSON.

        Records may be dicts or typed models (see as_dict). The table and
        its columns are made as needed (see the module docstring).
        """
        if deleted_by is not None and key is None:
            raise ValueError("deleted_by needs a key, to know which rows to remove")
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        with tempfile.TemporaryDirectory() as folder:
            run = _stage(records, Path(folder), key, batch_size)
            gone = None
            if deleted_by is not None:
                gone = deleted_by.deleted
                if gone is None:
                    raise ApiError(
                        "deleted_by hasn't finished a run: pass the Changes "
                        "whose run gave these records"
                    )
            with self._transaction() as connection:
                # Columns change before any row does: DuckDB keeps the old
                # versions of rows a transaction replaced, and retyping a
                # column would have to convert those too.
                stored = _fit_columns(connection, table, run.types)
                for part in run.parts:
                    _load(connection, table, part, run.types, stored, key)
                if gone and stored:
                    column = _matching(stored, key or "")
                    connection.execute(
                        f"DELETE FROM {_quote(table)} WHERE {_quote(column)} "
                        "IN (SELECT unnest(?))",
                        [gone],
                    )
        log_event(
            logger,
            logging.INFO,
            "stored",
            "Stored %s records in %r",
            f"{run.count:,}",
            table,
            data={"table": table, "records": run.count},
        )
        return run.count

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


@dataclass
class _Staged:
    """A run's records in temporary files, and the type each field needs."""

    parts: list[Path] = field(default_factory=list)
    types: dict[str, str | None] = field(default_factory=dict)
    count: int = 0


def _stage(
    records: Iterable[Any], folder: Path, key: str | None, batch_size: int
) -> _Staged:
    """Write `records` to files in `folder`, `batch_size` at a time, typing each field.

    A field's type covers every value it had in the run (None while it has
    been null throughout). With a key, each file holds one record per key,
    the last; later files replace earlier ones when loaded in order.
    """
    run = _Staged()
    stream = iter(records)
    while batch := list(islice(stream, batch_size)):
        rows = [as_dict(record) for record in batch]
        if key is not None:
            rows = list(_last_per_key(rows, key).values())
        for name in _column_names(rows):
            kind = _type_of(row.get(name) for row in rows)
            known = run.types.get(name)
            run.types[name] = (
                kind
                if known is None
                else known
                if kind is None
                else _widen(known, kind)
            )
        _refuse_case_clashes(run.types)  # across batches too
        part = folder / f"part-{len(run.parts)}.ndjson"
        with part.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, default=str) + "\n")
        run.parts.append(part)
        run.count += len(rows)
    return run


def _fit_columns(
    connection: "duckdb.DuckDBPyConnection",
    table: str,
    types: dict[str, str | None],
) -> dict[str, str]:
    """Make `table`'s columns hold a run of `types`, and return them, by name.

    Creates the table, adds missing columns, retypes columns that hold only
    nulls, and widens the rest, as the module docstring says. A field seen
    only as null gets a text column as a place holder.
    """
    stored = _columns(connection, table)
    if not stored:
        if not types:
            return {}
        columns = ", ".join(f"{_quote(n)} {t or 'VARCHAR'}" for n, t in types.items())
        connection.execute(f"CREATE TABLE {_quote(table)} ({columns})")
        return {n: t or "VARCHAR" for n, t in types.items()}
    for name, kind in types.items():
        existing = _matching(stored, name)
        if existing not in stored:
            connection.execute(
                f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(name)} "
                f"{kind or 'VARCHAR'}"
            )
            stored[name] = kind or "VARCHAR"
            continue
        if kind is None or kind == stored[existing]:
            continue
        if _only_nulls(connection, table, existing):
            # Nothing to keep yet, so the column takes the new type as is.
            wider = kind
        else:
            wider = _widen(stored[existing], kind)
            if wider == stored[existing]:
                continue
            log_event(
                logger,
                logging.WARNING,
                "widen",
                "Column %r of %r held %s and now gets %s; storing it as %s",
                existing,
                table,
                stored[existing],
                kind,
                wider,
                data={"table": table, "column": existing, "type": wider},
            )
        connection.execute(
            f"ALTER TABLE {_quote(table)} ALTER {_quote(existing)} TYPE {wider}"
        )
        stored[existing] = wider
    return stored


def _load(
    connection: "duckdb.DuckDBPyConnection",
    table: str,
    part: Path,
    types: dict[str, str | None],
    stored: dict[str, str],
    key: str | None,
) -> None:
    """Store one staged file in `table`, replacing rows by `key` if given."""
    spec = ", ".join(
        f"{_sql_text(name)}: {_sql_text(stored[_matching(stored, name)])}"
        for name in types
    )
    source = (
        f"read_json({_sql_text(str(part))}, format='newline_delimited', "
        f"columns={{{spec}}})"
    )
    if key is not None:
        connection.execute(
            f"DELETE FROM {_quote(table)} WHERE {_quote(_matching(stored, key))} "
            f"IN (SELECT {_quote(key)} FROM {source})"
        )
    connection.execute(f"INSERT INTO {_quote(table)} BY NAME SELECT * FROM {source}")


def _matching(stored: dict[str, str], name: str) -> str:
    """The stored column `name` lands in: DuckDB matches names without regard to case."""
    lowered = name.lower()
    return next((column for column in stored if column.lower() == lowered), name)


def _sql_text(text: str) -> str:
    """`text` as a DuckDB string literal."""
    return "'" + text.replace("'", "''") + "'"


def _columns(connection: "duckdb.DuckDBPyConnection", table: str) -> dict[str, str]:
    """`table`'s columns and their types, in order, or {} if there is no such table."""
    rows = connection.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? AND table_schema = current_schema() "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return dict(rows)


def _only_nulls(
    connection: "duckdb.DuckDBPyConnection", table: str, column: str
) -> bool:
    """Whether `column` of `table` holds no value but null."""
    found = connection.execute(
        f"SELECT count({_quote(column)}) FROM {_quote(table)}"
    ).fetchone()
    return found is not None and found[0] == 0


def _column_names(rows: list[dict[str, Any]]) -> list[str]:
    """Every field of `rows`, in the order first seen, refusing names that clash.

    DuckDB matches column names without regard to case, so "id" and "ID"
    would be one column; a batch holding both is refused rather than mixed.
    """
    names = list(dict.fromkeys(name for row in rows for name in row))
    _refuse_case_clashes(names)
    return names


def _refuse_case_clashes(names: Iterable[str]) -> None:
    """Raise ApiError if two of `names` differ only in case."""
    seen: dict[str, str] = {}
    for name in names:
        other = seen.setdefault(name.lower(), name)
        if other != name:
            raise ApiError(
                f"Fields {other!r} and {name!r} differ only in case, which "
                "DuckDB treats as one column"
            )


def _last_per_key(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """`rows` by their key, the last of each key winning, in first-seen order."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        found = row.get(key)
        if found is None:
            raise ApiError(
                f"A record has no key {key!r}: {json.dumps(row, default=str)[:200]}"
            )
        by_key[json.dumps(found, sort_keys=True, default=str)] = row
    return by_key


def _make_state_table(connection: "duckdb.DuckDBPyConnection") -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
        "name VARCHAR PRIMARY KEY, value VARCHAR NOT NULL, saved_at VARCHAR NOT NULL)"
    )
