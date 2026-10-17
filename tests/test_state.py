"""State stores and tracked_run: keeping an incremental run's place between runs.

The StateStore promises are checked once per store by check_state_store,
and its own tests show it catches each kind of broken store. The other
tests cover what only SqliteState does (a file that outlives the object,
refusing what it can't keep) and the one promise of tracked_run: the
state is saved only when the whole block worked.
"""

import math
import sys

import pytest

from api_explorer import Incremental, SqliteState, StateStore, tracked_run
from api_explorer.testing import MemoryState, changes_api, check_state_store


def rec(id_: int, day: int) -> dict:
    return {"id": id_, "updated_at": f"2026-01-{day:02d}T00:00:00Z"}


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path) -> StateStore:
    return (
        SqliteState(tmp_path / "state.db")
        if request.param == "sqlite"
        else MemoryState()
    )


def test_the_built_in_stores_keep_every_state_store_promise(store):
    assert isinstance(store, StateStore)
    check_state_store(store)


class SameObject(MemoryState):
    """Broken: hands out the stored object itself."""

    def __init__(self):
        super().__init__()
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def put(self, name, value):
        self.values[name] = value


class FirstWins(MemoryState):
    """Broken: never replaces a value once saved."""

    def put(self, name, value):
        if self.get(name) is None:
            super().put(name, value)


class OneSlot(MemoryState):
    """Broken: every name shares one value."""

    def get(self, name):
        return super().get("slot")

    def put(self, name, value):
        super().put("slot", value)


class NeverEmpty(MemoryState):
    """Broken: gives an empty dict, not None, for a name never saved."""

    def get(self, name):
        found = super().get(name)
        return {} if found is None else found


@pytest.mark.parametrize(
    ("store", "message"),
    [
        (SameObject(), "not a copy"),
        (FirstWins(), "didn't replace"),
        (OneSlot(), "changed 'check-state-store-b'"),
        (NeverEmpty(), "must give None for a name never saved"),
    ],
    ids=["same-object", "first-wins", "one-slot", "never-empty"],
)
def test_check_state_store_catches_a_broken_store(store, message):
    with pytest.raises(AssertionError, match=message):
        check_state_store(store)


# SqliteState


def test_a_sqlite_state_outlives_the_object_that_saved_it(tmp_path):
    SqliteState(tmp_path / "state.db").put("orders", {"last": 5})
    assert SqliteState(tmp_path / "state.db").get("orders") == {"last": 5}


@pytest.mark.parametrize(
    "value", [{"when": object()}, {"n": math.nan}], ids=["object", "nan"]
)
def test_a_sqlite_state_refuses_what_isnt_json_and_keeps_the_old_value(tmp_path, value):
    store = SqliteState(tmp_path / "state.db")
    store.put("orders", {"last": 5})
    with pytest.raises((TypeError, ValueError)):
        store.put("orders", value)
    assert store.get("orders") == {"last": 5}


@pytest.mark.parametrize("path", [":memory:", "file::memory:?cache=shared"])
def test_a_sqlite_state_needs_a_file(path):
    with pytest.raises(ValueError, match="MemoryState"):
        SqliteState(path)


@pytest.mark.parametrize("name", ["", "  ", None], ids=["empty", "spaces", "none"])
def test_a_state_name_must_be_text(tmp_path, name):
    with pytest.raises(ValueError, match="non-empty text"):
        SqliteState(tmp_path / "state.db").get(name)


def test_without_sqlite3_the_error_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite3", None)
    with pytest.raises(ImportError, match="built without"):
        SqliteState(tmp_path / "state.db").get("orders")


# tracked_run


def test_a_tracked_run_saves_its_place_and_the_next_run_starts_there(store):
    data = [rec(1, 1), rec(2, 2)]
    api = changes_api(data)
    with tracked_run(store, "orders", "updated_at", "updated_since") as since:
        first = list(api.client().records("/orders/", incremental=since))
    data.append(rec(3, 3))
    with tracked_run(store, "orders", "updated_at", "updated_since") as since:
        second = list(api.client().records("/orders/", incremental=since))
    assert [r["id"] for r in first] == [1, 2]
    assert [r["id"] for r in second] == [3]
    assert api.queries[-1] == {"updated_since": "2026-01-02T00:00:00Z"}


def test_a_tracked_run_whose_storing_fails_saves_nothing():
    # The error comes from the caller's own storing, after every record
    # was fetched: the state must still stay put.
    store = MemoryState()
    api = changes_api([rec(1, 1)])
    with (
        pytest.raises(OSError, match="disk full"),
        tracked_run(store, "orders", "updated_at") as since,
    ):
        list(api.client().records("/orders/", incremental=since))
        raise OSError("disk full")
    assert store.saves == 0
    with tracked_run(store, "orders", "updated_at") as since:
        again = list(api.client().records("/orders/", incremental=since))
    assert again == [rec(1, 1)]


def test_a_tracked_run_passes_its_options_to_incremental():
    store = MemoryState()
    with tracked_run(
        store, "orders", "updated_at", key="id", start="2026-01-02"
    ) as since:
        assert isinstance(since, Incremental)
        assert list(since.track([rec(1, 1), rec(2, 2)])) == [rec(2, 2)]
    # Record 2 equals the start, so the start's own form stays as "last".
    assert store.get("orders") == {"last": "2026-01-02", "seen": ["2"]}


def test_a_tracked_run_that_stops_reading_early_keeps_its_old_place():
    store = MemoryState({"orders": {"last": "2026-01-01T00:00:00Z", "seen": []}})
    with tracked_run(store, "orders", "updated_at") as since:
        records = since.track([rec(1, 2), rec(2, 3)])
        next(records)
        records.close()
    assert store.get("orders") == {"last": "2026-01-01T00:00:00Z", "seen": []}
