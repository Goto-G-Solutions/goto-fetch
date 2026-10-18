"""Where fetched records are kept: the Warehouse interface, and the rules warehouses share.

A Warehouse stores records in named tables and reads them back. Its
promises are about safety, not features (testing.check_warehouse checks
them):

- One write() is all or nothing. If the records fail part way (a failed
  request) or storing fails, the table is left as it was. Changes.deleted
  relies on this.
- With a key, a record replaces the row with its key, and of several with
  one key in a write, the last wins. Without one, records are added.
- deleted_by's keys are removed only once the records are stored, and a
  tracker whose run isn't complete is refused before anything is written.
- No value is lost. read() gives each value back as written, or, where a
  column had to change type, as its JSON text ("1", "true", '{"a":1}');
  text stays text. A record without its key, or with no fields at all, is
  refused, never stored.

How tables and columns are made is up to each warehouse. The ones here
are DuckDBWarehouse (the "duckdb" extra), SqliteWarehouse (Python's own
sqlite3) and, for tests, testing.MemoryWarehouse.

The SQL ones are built on write_run, which keeps the promises for any
SQL database that implements SqlTables: it reads a whole run into
temporary files first, so a failed request never touches the database,
gives each column one of a few kinds (Kind), widening it when its values
change kind, and changes columns before any row. A warehouse for another
database implements SqlTables and calls write_run; the helpers here
(kind_of, widen, to_kind, as_text, quote, matching, read_part and the
checks) are its building blocks.
"""

import json
import logging
import math
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from .errors import ApiError
from .incremental import ReportsDeletions
from .logs import log_event
from .models import as_dict

logger = logging.getLogger(__name__)


@runtime_checkable
class Warehouse(Protocol):
    """Tables of records: write a run into one, all or nothing, and read it back.

    See the module docstring for the promises every warehouse keeps. `key`
    is a top-level field of the records, such as "id" (not a path).
    """

    def write(
        self,
        table: str,
        records: Iterable[Any],
        *,
        key: str | None = None,
        deleted_by: ReportsDeletions | None = None,
    ) -> int:
        """Store `records` in `table`, all or nothing; return how many were stored."""
        ...

    def read(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """`table`'s rows as dicts (at most `limit`), in no set order; [] if none.

        A negative limit is refused with ValueError (check_limit).
        """
        ...


Kind = Literal["boolean", "integer", "float", "text", "json"]
"""What a column holds, whatever the database calls it."""

INT64 = range(-(2**63), 2**63)
FLOAT_EXACT = 2**53
"""The largest integer a float holds exactly: beyond it, a float would lose digits."""


def kind_of(values: Iterable[Any]) -> Kind | None:
    """The kind of column that holds every one of `values`, or None if all are null.

    Nested objects and lists are "json". Mixed kinds give "text", except
    integers beside floats, which give "float" if a float holds every one
    of them exactly. Integers beyond 64 bits give "text", so no digit is
    lost.
    """
    kinds: set[Kind] = set()
    exact = True
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add("boolean")
        elif isinstance(value, int):
            kinds.add("integer" if value in INT64 else "text")
            exact = exact and abs(value) <= FLOAT_EXACT
        elif isinstance(value, float):
            kinds.add("float")
        elif isinstance(value, (dict, list)):
            kinds.add("json")
        else:
            kinds.add("text")
    if not kinds:
        return None
    if len(kinds) == 1:
        return kinds.pop()
    return widen(*sorted(kinds), exact=exact) if len(kinds) == 2 else "text"


def widen(stored: Kind, incoming: Kind, *, exact: bool = True) -> Kind:
    """The kind a column needs to keep its `stored` values and take `incoming` ones.

    Integers beside floats give "float", but only if `exact`: every integer
    among them fits a float exactly (see FLOAT_EXACT). Otherwise, and for
    any other mix, "text", so no value is lost.
    """
    if stored == incoming:
        return stored
    if {stored, incoming} == {"integer", "float"}:
        return "float" if exact else "text"
    return "text"


def has_inexact_integers(values: Iterable[Any]) -> bool:
    """Whether some integer among `values` is too large for a float to hold exactly."""
    return any(
        isinstance(v, int) and not isinstance(v, bool) and abs(v) > FLOAT_EXACT
        for v in values
    )


def to_kind(value: Any, kind: Kind) -> Any:
    """`value` as a column of `kind` holds it, or None if no such column could hold it.

    For loading values and for matching keys against a column: a key of
    the wrong kind can't be in the column, so it matches nothing.
    """
    if value is None:
        return None
    if kind == "text":
        return as_text(value)
    if kind == "json":
        return json.dumps(value, separators=(",", ":"), default=str)
    if isinstance(value, bool):
        return value if kind == "boolean" else None
    if kind == "float" and isinstance(value, (int, float)):
        # SQLite stores NaN and infinity as NULL; their JSON text keeps them.
        return float(value) if math.isfinite(value) else as_text(value)
    if kind == "integer" and isinstance(value, int):
        return value
    return None


def as_text(value: Any) -> str | None:
    """`value` as a text column holds it: text as is, anything else as JSON text."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), default=str)


def same_value(read: Any, written: Any) -> bool:
    """Whether `read` gives `written` back, as warehouses promise: as is, or as JSON text."""
    return read == written or (isinstance(read, str) and read == as_text(written))


def quote(name: str) -> str:
    """`name` as an SQL identifier, safe whatever it holds."""
    return '"' + name.replace('"', '""') + '"'


def matching(columns: Iterable[str], name: str) -> str:
    """The column `name` lands in, matched without regard to case, as SQL does."""
    lowered = name.lower()
    return next((column for column in columns if column.lower() == lowered), name)


def keys_of_kind(keys: Iterable[Any], kind: Kind) -> list[Any]:
    """`keys` as a key column of `kind` holds them, leaving out any it couldn't hold.

    A key of another kind can't be in the column, so it would match no row.
    """
    converted = (to_kind(key, kind) for key in keys)
    return [key for key in converted if key is not None]


def check_limit(limit: int | None) -> None:
    """Refuse a negative read limit, which each database would read differently."""
    if limit is not None and limit < 0:
        raise ValueError(f"limit can't be negative, got {limit}")


def check_write(key: str | None, deleted_by: ReportsDeletions | None) -> None:
    """Refuse write() arguments that can't work, before reading any record."""
    if deleted_by is not None and key is None:
        raise ValueError("deleted_by needs a key, to know which rows to remove")


def deleted_keys(deleted_by: ReportsDeletions | None) -> list[Any]:
    """The keys to remove after storing, refusing a tracker whose run isn't complete."""
    if deleted_by is None:
        return []
    gone = deleted_by.deleted
    if gone is None:
        # A caller's mistake, like check_write's, so a ValueError.
        raise ValueError(
            "deleted_by hasn't finished a run: pass the tracker whose run gave "
            "these records"
        )
    return list(gone)


def refuse_empty_records(rows: Iterable[dict[str, Any]]) -> None:
    """Raise ApiError if a row has no fields: nothing to store, and no column to make."""
    if any(not row for row in rows):
        raise ApiError("A record has no fields, so there is nothing to store: {}")


def last_per_key(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """`rows`, one per key, the last of each winning, in first-seen order."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        found = row.get(key)
        if found is None:
            raise ApiError(
                f"A record has no key {key!r}: {json.dumps(row, default=str)[:200]}"
            )
        by_key[json.dumps(found, sort_keys=True, default=str)] = row
    return list(by_key.values())


def refuse_case_clashes(names: Iterable[str]) -> None:
    """Raise ApiError if two of `names` differ only in case.

    SQL matches column names without regard to case, so "id" and "ID" would
    be one column; records holding both are refused rather than mixed.
    """
    seen: dict[str, str] = {}
    for name in names:
        other = seen.setdefault(name.lower(), name)
        if other != name:
            raise ApiError(
                f"Fields {other!r} and {name!r} differ only in case, which a "
                "database treats as one column"
            )


@dataclass
class _Staged:
    """A run's records in temporary files, and the kind each field needs."""

    parts: list[Path] = field(default_factory=list)
    kinds: dict[str, Kind | None] = field(default_factory=dict)
    inexact: set[str] = field(default_factory=set)
    count: int = 0


def _stage(
    records: Iterable[Any], folder: Path, key: str | None, batch_size: int
) -> _Staged:
    """Write `records` to files in `folder`, `batch_size` at a time, giving each field a kind.

    A field's kind covers every value it had in the run (None while it has
    been null throughout). With a key, each file holds one record per key,
    the last; loading the files in order lets later ones replace earlier.
    """
    run = _Staged()
    stream = iter(records)
    while batch := list(islice(stream, batch_size)):
        rows = [as_dict(record) for record in batch]
        refuse_empty_records(rows)
        if key is not None:
            rows = last_per_key(rows, key)
        names = list(dict.fromkeys(name for row in rows for name in row))
        for name in names:
            values = [row.get(name) for row in rows]
            if has_inexact_integers(values):
                run.inexact.add(name)
            kind = kind_of(values)
            known = run.kinds.get(name)
            if known is None:
                run.kinds[name] = kind
            elif kind is not None:
                run.kinds[name] = widen(known, kind, exact=name not in run.inexact)
        refuse_case_clashes(run.kinds)
        part = folder / f"part-{len(run.parts)}.ndjson"
        with part.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, default=str) + "\n")
        run.parts.append(part)
        run.count += len(rows)
    return run


def _plan_columns(
    stored: dict[str, Kind],
    run: _Staged,
    only_nulls: Callable[[str], bool],
    holds_inexact: Callable[[str], bool],
    table: str,
) -> tuple[dict[str, Kind], dict[str, Kind], dict[str, Kind]]:
    """Work out how a table's columns must change to take a run.

    `stored` maps the table's columns to their kinds ({} if there is no
    table). only_nulls(column) says whether a column holds nothing but
    nulls, and holds_inexact(column) whether an integer column holds a
    value too large for a float to keep exactly (asked only when floats
    arrive in one). Returns three maps: the columns to add, the columns to
    change kind (each logged as a "widen" warning, unless it held only
    nulls), and every column after. A field seen only as null gets a text
    column as a place holder.
    """
    after = dict(stored)
    added: dict[str, Kind] = {}
    changed: dict[str, Kind] = {}
    for name, kind in run.kinds.items():
        existing = matching(after, name)
        if existing not in after:
            added[name] = after[name] = kind or "text"
            continue
        if kind is None or kind == after[existing]:
            continue
        if only_nulls(existing):
            new = kind  # nothing to keep yet, so the column takes the new kind
        else:
            pair = {after[existing], kind}
            # Only an integer column can hold an integer a float can't.
            exact = pair != {"integer", "float"} or (
                name not in run.inexact
                and (after[existing] != "integer" or not holds_inexact(existing))
            )
            new = widen(after[existing], kind, exact=exact)
            if new == after[existing]:
                continue
            log_event(
                logger,
                logging.WARNING,
                "widen",
                "Column %r of %r held %s and now gets %s; storing it as %s",
                existing,
                table,
                after[existing],
                kind,
                new,
                data={"table": table, "column": existing, "type": new},
            )
        changed[existing] = after[existing] = new
    return added, changed, after


def _log_stored(table: str, count: int) -> None:
    """Log the "stored" event a write ends with."""
    log_event(
        logger,
        logging.INFO,
        "stored",
        "Stored %s records in %r",
        f"{count:,}",
        table,
        data={"table": table, "records": count},
    )


def read_part(part: Path) -> Iterator[dict[str, Any]]:
    """The records of one staged file, as write_run hands them to SqlTables.load."""
    with part.open(encoding="utf-8") as file:
        for line in file:
            yield json.loads(line)


class SqlTables(Protocol):
    """What write_run needs from an SQL database: the steps that differ between them.

    write_run owns the order and the decisions; these only carry them out,
    inside the connection transaction() gives, which must be a DB-API
    connection (execute(sql).fetchone() works on it).
    """

    def transaction(self) -> AbstractContextManager[Any]:
        """Open one transaction: commit when the block ends, roll back on an error."""
        ...

    def columns(self, connection: Any, table: str) -> dict[str, Kind]:
        """`table`'s columns and their kinds, in order; {} if there is no such table."""
        ...

    def alter(
        self,
        connection: Any,
        table: str,
        stored: dict[str, Kind],
        added: dict[str, Kind],
        changed: dict[str, Kind],
        after: dict[str, Kind],
    ) -> None:
        """Make `table` hold the columns in `after`: create it if `stored` is empty,
        else add the `added` columns and change the kind of the `changed` ones,
        converting their values (see to_kind)."""
        ...

    def load(
        self,
        connection: Any,
        table: str,
        part: Path,
        fields: list[str],
        columns: dict[str, Kind],
        key: str | None,
    ) -> None:
        """Store one staged file (read_part reads it) in `table`.

        With a key, first remove the rows whose key the file holds. Each
        field goes in the column matching it (see matching), as that
        column's kind holds it (see to_kind).
        """
        ...

    def delete(self, connection: Any, table: str, column: str, keys: list[Any]) -> None:
        """Remove the rows whose `column` holds one of `keys` (already in its kind)."""
        ...


def write_run(
    tables: SqlTables,
    table: str,
    records: Iterable[Any],
    *,
    key: str | None = None,
    deleted_by: ReportsDeletions | None = None,
    batch_size: int = 10_000,
) -> int:
    """Store `records` in `table`, keeping every Warehouse promise; return how many.

    The order is what keeps them, so it lives here once:

    1. Read the whole run into temporary files, `batch_size` records at a
       time, working out each field's kind. A failure here (a failed
       request) never opens the database.
    2. In one transaction, change the columns first: DuckDB keeps the old
       versions of rows a transaction replaced, so retyping a column after
       writing rows can fail on values no longer visible.
    3. Store each file in order, so later records replace earlier ones.
    4. Remove deleted_by's keys, now that the records are stored.

    The temporary files need about as much disk as the run's records as
    JSON.
    """
    check_write(key, deleted_by)
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    with tempfile.TemporaryDirectory() as folder:
        run = _stage(records, Path(folder), key, batch_size)
        gone = deleted_keys(deleted_by)
        with tables.transaction() as connection:

            def count(sql: str) -> int:
                found = connection.execute(sql).fetchone()
                return 0 if found is None else int(found[0])

            stored = tables.columns(connection, table)
            added, changed, after = _plan_columns(
                stored,
                run,
                lambda column: (
                    count(f"SELECT count({quote(column)}) FROM {quote(table)}") == 0
                ),
                lambda column: (
                    count(
                        f"SELECT count(*) FROM {quote(table)} "
                        f"WHERE abs({quote(column)}) > {FLOAT_EXACT}"
                    )
                    > 0
                ),
                table,
            )
            if after:
                tables.alter(connection, table, stored, added, changed, after)
                fields = list(run.kinds)
                for part in run.parts:
                    tables.load(connection, table, part, fields, after, key)
                if gone:
                    column = matching(after, key or "")
                    if keys := keys_of_kind(gone, after[column]):
                        tables.delete(connection, table, column, keys)
    _log_stored(table, run.count)
    return run.count
