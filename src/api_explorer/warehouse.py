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
  text stays text. A record without its key is refused, never stored.

How tables and columns are made is up to each warehouse. The ones here
are DuckDBWarehouse (the "duckdb" extra), SqliteWarehouse (Python's own
sqlite3) and, for tests, testing.MemoryWarehouse. The database ones share
the rules below: they read a whole run into temporary files first, so a
failed request never touches the database, and give each column one of a
few kinds, widening it when its values change kind.
"""

import json
import logging
from collections.abc import Iterable
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

    See the module docstring for the promises every warehouse keeps.
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
        """`table`'s rows as dicts (at most `limit`), in no set order; [] if none."""
        ...


Kind = Literal["boolean", "integer", "float", "text", "json"]
"""What a column holds, whatever the database calls it."""

INT64 = range(-(2**63), 2**63)


def kind_of(values: Iterable[Any]) -> Kind | None:
    """The kind of column that holds every one of `values`, or None if all are null.

    Nested objects and lists are "json". Mixed kinds give "text", except
    integers beside floats, which give "float". Integers beyond 64 bits
    give "text", so no digit is lost.
    """
    kinds: set[Kind] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add("boolean")
        elif isinstance(value, int):
            kinds.add("integer" if value in INT64 else "text")
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
    return "float" if kinds == {"integer", "float"} else "text"


def widen(stored: Kind, incoming: Kind) -> Kind:
    """The kind a column needs to keep its `stored` values and take `incoming` ones."""
    if stored == incoming:
        return stored
    if {stored, incoming} == {"integer", "float"}:
        return "float"
    return "text"


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


def check_write(
    key: str | None, deleted_by: ReportsDeletions | None, batch_size: int
) -> None:
    """Refuse write() arguments that can't work, before reading any record."""
    if deleted_by is not None and key is None:
        raise ValueError("deleted_by needs a key, to know which rows to remove")
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")


def deleted_keys(deleted_by: ReportsDeletions | None) -> list[Any]:
    """The keys to remove after storing, refusing a tracker whose run isn't complete."""
    if deleted_by is None:
        return []
    gone = deleted_by.deleted
    if gone is None:
        raise ApiError(
            "deleted_by hasn't finished a run: pass the tracker whose run gave "
            "these records"
        )
    return list(gone)


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
class Staged:
    """A run's records in temporary files, and the kind each field needs."""

    parts: list[Path] = field(default_factory=list)
    kinds: dict[str, Kind | None] = field(default_factory=dict)
    count: int = 0

    def rows(self, part: Path) -> Iterable[dict[str, Any]]:
        """The records of one staged file."""
        with part.open(encoding="utf-8") as file:
            for line in file:
                yield json.loads(line)


def stage(
    records: Iterable[Any], folder: Path, key: str | None, batch_size: int
) -> Staged:
    """Write `records` to files in `folder`, `batch_size` at a time, giving each field a kind.

    A field's kind covers every value it had in the run (None while it has
    been null throughout). With a key, each file holds one record per key,
    the last; loading the files in order lets later ones replace earlier.
    """
    run = Staged()
    stream = iter(records)
    while batch := list(islice(stream, batch_size)):
        rows = [as_dict(record) for record in batch]
        if key is not None:
            rows = last_per_key(rows, key)
        names = list(dict.fromkeys(name for row in rows for name in row))
        for name in names:
            kind = kind_of(row.get(name) for row in rows)
            known = run.kinds.get(name)
            if known is None:
                run.kinds[name] = kind
            elif kind is not None:
                run.kinds[name] = widen(known, kind)
        refuse_case_clashes(run.kinds)
        part = folder / f"part-{len(run.parts)}.ndjson"
        with part.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, default=str) + "\n")
        run.parts.append(part)
        run.count += len(rows)
    return run


def plan_columns(
    stored: dict[str, Kind],
    incoming: dict[str, Kind | None],
    only_nulls: Any,
    table: str,
) -> tuple[dict[str, Kind], dict[str, Kind], dict[str, Kind]]:
    """Work out how a table's columns must change to take a run.

    `stored` maps the table's columns to their kinds ({} if there is no
    table), `incoming` the run's fields to theirs, and only_nulls(column)
    says whether a column holds nothing but nulls. Returns three maps:
    the columns to add, the columns to change kind (each logged as a
    "widen" warning, unless it held only nulls), and every column after.
    A field seen only as null gets a text column as a place holder.
    """
    after = dict(stored)
    added: dict[str, Kind] = {}
    changed: dict[str, Kind] = {}
    for name, kind in incoming.items():
        existing = matching(after, name)
        if existing not in after:
            added[name] = after[name] = kind or "text"
            continue
        if kind is None or kind == after[existing]:
            continue
        if only_nulls(existing):
            new = kind  # nothing to keep yet, so the column takes the new kind
        else:
            new = widen(after[existing], kind)
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


def log_stored(table: str, count: int) -> None:
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
