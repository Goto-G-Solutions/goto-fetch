"""Keep a tracker's state between runs: a small store of named JSON values.

A StateStore holds one JSON value per name, such as "orders". SqliteState
keeps them in an SQLite file, using Python's own sqlite3 module, so it
needs no extra package. tracked_run ties the steps of a tracked run
together, so the state is saved only when the run and the storing both
succeed.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from .incremental import Tracker

if TYPE_CHECKING:
    import sqlite3

T = TypeVar("T", bound=Tracker)


@runtime_checkable
class StateStore(Protocol):
    """Where a tracked run's state lives between runs.

    The promises (testing.check_state_store checks them): get gives None
    for a name never saved, and after put, get gives back an equal value
    (a copy, not the same object); put replaces what the name held; names
    don't affect each other. Values are plain JSON: dicts, lists, text,
    numbers, booleans and None.
    """

    def get(self, name: str) -> Any:
        """The value last saved under `name`, or None."""
        ...

    def put(self, name: str, value: Any) -> None:
        """Save `value` under `name`, replacing what was there."""
        ...


STATE_TABLE = "goto_fetch_state"
"""The table the built-in stores keep state in, in a database of their own or a warehouse's."""


def check_state_name(name: str) -> None:
    """Refuse a state name that isn't non-empty text."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"A state name must be non-empty text, got {name!r}")


class SqliteState:
    """A StateStore in an SQLite file, one row per name.

    Each call opens the file, does its work in one transaction and closes
    it, so a crash leaves either the old value or the new one, never half
    of either, and several processes can share the file. The table,
    `goto_fetch_state`, is made on first use; the file's folder must
    exist.

    Two runs under one name at once are safe but wasteful: the last to
    finish wins, and if its value is the older one, the next run fetches
    some records again. It never skips any.
    """

    TABLE = STATE_TABLE

    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
        if str(path) == ":memory:" or str(path).startswith("file::memory:"):
            # Each call opens a new connection, which would get an empty
            # database every time. testing.MemoryState is the in-memory store.
            raise ValueError(
                "SqliteState needs a file; for a store in memory, use "
                "goto_fetch.testing.MemoryState"
            )
        self.path = Path(path)
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"SqliteState({str(self.path)!r})"

    def get(self, name: str) -> Any:
        """The value last saved under `name`, or None."""
        check_state_name(name)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT value FROM {self.TABLE} WHERE name = ?",
                (name,),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, name: str, value: Any) -> None:
        """Save `value` under `name`, replacing what was there.

        Raises TypeError for a value that isn't plain JSON, before
        touching the file.
        """
        check_state_name(name)
        text = json.dumps(value, allow_nan=False)
        saved_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {self.TABLE} (name, value, saved_at) "
                "VALUES (?, ?, ?) ON CONFLICT (name) DO UPDATE "
                "SET value = excluded.value, saved_at = excluded.saved_at",
                (name, text, saved_at),
            )

    @contextmanager
    def _connect(self) -> Iterator["sqlite3.Connection"]:
        """Open the file for one transaction: commit on success, roll back on error."""
        try:
            import sqlite3
        except ImportError as exc:  # some custom Python builds leave it out
            raise ImportError(
                "SqliteState needs Python's sqlite3 module, which this Python "
                "was built without; use another StateStore"
            ) from exc
        # closing() closes the file; the connection's own `with` makes the
        # block one transaction, committed on success, rolled back on error.
        with (
            closing(sqlite3.connect(self.path, timeout=self.timeout)) as connection,
            connection,
        ):
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} ("
                "name TEXT PRIMARY KEY, value TEXT NOT NULL, saved_at TEXT NOT NULL)"
            )
            yield connection


@contextmanager
def tracked_run(
    store: StateStore,
    name: str,
    tracker: Callable[..., T],
    *args: Any,
    **options: Any,
) -> Iterator[T]:
    """Run one tracked fetch, saving its state only if everything in the block worked.

    Builds `tracker(*args, state=..., **options)` from `store`'s state for
    `name`: Incremental, Changes, or any Tracker class that takes `state=`.
    Fetch and store the records inside the block; when the block ends
    without an error, the new state is saved. An error anywhere, in the
    fetch or in your storing, leaves the saved state as it was, so the next
    run fetches the same records again rather than skip them.

    Read the records inside the block, by storing them or with list():
    records() is lazy, so records read after the block ends were never part
    of the tracked run, its state doesn't move, and the next run fetches
    them again:

        store = SqliteState("state.db")
        with tracked_run(store, "orders", Incremental, "updated_at",
                         "updated_since") as since:
            save(client.records("/orders/", incremental=since))
    """
    check_state_name(name)
    made = tracker(*args, state=store.get(name), **options)
    yield made
    store.put(name, made.state)
