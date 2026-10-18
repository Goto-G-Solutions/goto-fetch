"""Every interface, and the pieces that fit it: ours, and one built as a user would.

Unlike the other test files, pyright checks this one (see pyproject.toml),
so the assignments below prove at type-check time that each built-in
piece still fits its interface, and that a piece written only against an
interface, never inheriting from ours, fits too. The tests then use the
user-built tracker everywhere a built-in one goes.
"""

from collections.abc import Iterable, Iterator
from typing import Any

import pytest

from api_explorer import (
    ApiError,
    Changes,
    ClientCredentials,
    Cursor,
    Decoder,
    Fingerprint,
    HeaderLink,
    Incremental,
    Keyset,
    Login,
    NextLink,
    Offset,
    PageRequest,
    Paging,
    PasswordLogin,
    ReportsDeletions,
    ResolvesUrls,
    ResponseCheck,
    Retry,
    RetryPolicy,
    SqliteState,
    StateStore,
    TokenRefresh,
    Tracker,
    error_at,
    json_decoder,
    tracked_run,
)
from api_explorer.testing import MemoryState, check_tracker, next_link_api


class NewIds:
    """A tracker built only against the Tracker and ReportsDeletions interfaces.

    It delivers records whose id it hasn't seen, and reports ids that
    disappeared, the way a user might track an API whose records never
    change once made.
    """

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self._ids: list[Any] = list((state or {}).get("ids", []))
        self._state: dict[str, Any] = {"ids": list(self._ids)}
        self._deleted: list[Any] | None = None

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    @property
    def deleted(self) -> list[Any] | None:
        return self._deleted

    def apply(self, request: PageRequest) -> PageRequest:
        return request

    def track(self, records: Iterable[Any]) -> Iterator[Any]:
        seen: list[Any] = []
        for record in records:
            seen.append(record["id"])
            if record["id"] not in self._ids:
                yield record
        self._state = {"ids": seen}
        self._deleted = [i for i in self._ids if i not in seen]


def test_built_in_pieces_fit_their_interfaces(tmp_path):
    # Checked by pyright: each assignment fails type checking if a piece
    # drifts from its interface.
    paging: list[Paging] = [
        NextLink(),
        Offset(),
        HeaderLink(),
        Cursor(),
        Keyset("updated_at", "updated_since"),
    ]
    policy: RetryPolicy = Retry()
    logins: list[Login] = [
        PasswordLogin("/token/", "me", "pw"),
        ClientCredentials("/token/", "id", "secret"),
    ]
    resolves: ResolvesUrls = TokenRefresh("/token/refresh/", refresh="r")
    stores: list[StateStore] = [SqliteState(tmp_path / "s.db"), MemoryState()]
    trackers: list[Tracker] = [Incremental("updated_at"), Changes("id")]
    reports: ReportsDeletions = Changes("id")
    check: ResponseCheck = error_at()
    decode: Decoder = json_decoder()
    fingerprint: Fingerprint = repr
    # And at run time, for the interfaces isinstance can check.
    assert all(isinstance(s, StateStore) for s in stores)
    assert all(isinstance(t, Tracker) for t in trackers)
    assert isinstance(reports, ReportsDeletions)
    assert isinstance(resolves, ResolvesUrls)
    del paging, policy, logins, check, decode, fingerprint  # for pyright only


def test_a_user_built_tracker_fits_the_interfaces():
    tracker: Tracker = NewIds()
    reports: ReportsDeletions = NewIds()
    assert isinstance(tracker, Tracker)
    assert isinstance(reports, ReportsDeletions)
    assert not isinstance(Incremental("t"), ReportsDeletions)


def test_a_user_built_tracker_keeps_every_tracker_promise():
    check_tracker(NewIds, [{"id": 1}, {"id": 2}, {"id": 3}])


def test_a_user_built_tracker_runs_where_a_built_in_one_does():
    store = MemoryState()
    data = [{"id": i} for i in range(5)]
    with tracked_run(store, "items", NewIds) as first:
        assert (
            len(
                list(
                    next_link_api(data, page_size=2)
                    .client()
                    .records("/i/", incremental=first)
                )
            )
            == 5
        )
    data.append({"id": 9})
    del data[0]
    with tracked_run(store, "items", NewIds) as second:
        got = list(
            next_link_api(data, page_size=2).client().records("/i/", incremental=second)
        )
    assert got == [{"id": 9}]
    assert second.deleted == [0]


def test_a_user_built_tracker_s_deletions_reach_the_warehouse(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from api_explorer.warehouse import Warehouse

    shop = Warehouse(tmp_path / "shop.duckdb")
    first = NewIds()
    shop.write("items", first.track([{"id": 1}, {"id": 2}]), key="id", deleted_by=first)
    second = NewIds(first.state)
    shop.write("items", second.track([{"id": 2}]), key="id", deleted_by=second)
    with duckdb.connect(str(shop.path)) as connection:
        assert connection.sql("SELECT id FROM items").fetchall() == [(2,)]


def test_a_tracker_that_reports_nothing_yet_stops_the_write(tmp_path):
    pytest.importorskip("duckdb")
    from api_explorer.warehouse import Warehouse

    with pytest.raises(ApiError, match="hasn't finished a run"):
        Warehouse(tmp_path / "shop.duckdb").write(
            "items", [{"id": 1}], key="id", deleted_by=NewIds()
        )
