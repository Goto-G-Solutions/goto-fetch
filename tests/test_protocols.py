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
    Auth,
    Changes,
    ClientCredentials,
    Cursor,
    Decoder,
    DuckDBWarehouse,
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
    Response,
    ResponseCheck,
    Retry,
    RetryPolicy,
    SqliteState,
    SqliteWarehouse,
    SqlTables,
    StateStore,
    TokenRefresh,
    Tracker,
    Transport,
    Urllib3Transport,
    Warehouse,
    error_at,
    json_decoder,
    tracked_run,
)
from api_explorer.testing import (
    FakeApi,
    MemoryState,
    MemoryWarehouse,
    check_tracker,
    next_link_api,
)


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
    auths: list[Auth] = [TokenRefresh("/token/refresh/", refresh="r")]
    transports: list[Transport] = [
        Urllib3Transport(),
        FakeApi(lambda request: Response(200)),
    ]
    stores: list[StateStore] = [SqliteState(tmp_path / "s.db"), MemoryState()]
    trackers: list[Tracker] = [Incremental("updated_at"), Changes("id")]
    warehouses: list[Warehouse] = [
        DuckDBWarehouse(tmp_path / "w.duckdb"),
        SqliteWarehouse(tmp_path / "w.db"),
        MemoryWarehouse(),
    ]
    stores += [w for w in warehouses if isinstance(w, StateStore)]
    reports: ReportsDeletions = Changes("id")
    tables: list[SqlTables] = [
        DuckDBWarehouse(tmp_path / "t.duckdb")._tables,
        SqliteWarehouse(tmp_path / "t.db")._tables,
    ]
    check: ResponseCheck = error_at()
    decode: Decoder = json_decoder()
    fingerprint: Fingerprint = repr
    # And at run time, for the interfaces isinstance can check.
    assert all(isinstance(s, StateStore) for s in stores)
    assert all(isinstance(t, Tracker) for t in trackers)
    assert all(isinstance(w, Warehouse) for w in warehouses)
    assert all(isinstance(w, StateStore) for w in warehouses)
    assert isinstance(reports, ReportsDeletions)
    assert isinstance(resolves, ResolvesUrls)
    assert all(isinstance(t, Transport) for t in transports)
    assert all(isinstance(a, Auth) for a in auths)
    del paging, policy, logins, check, decode, fingerprint, tables  # for pyright
    for transport in transports:
        transport.close()


def test_the_scraping_fields_fit_their_interface():
    pytest.importorskip("parsel")
    from api_explorer.scrape import CSSField, FieldExtractor, XPathField

    fields: list[FieldExtractor] = [CSSField("h2"), XPathField("//h2/text()")]
    del fields  # for pyright


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
    shop = DuckDBWarehouse(tmp_path / "shop.duckdb")
    first = NewIds()
    shop.write("items", first.track([{"id": 1}, {"id": 2}]), key="id", deleted_by=first)
    second = NewIds(first.state)
    shop.write("items", second.track([{"id": 2}]), key="id", deleted_by=second)
    with duckdb.connect(str(shop.path)) as connection:
        assert connection.sql("SELECT id FROM items").fetchall() == [(2,)]


def test_a_tracker_that_reports_nothing_yet_stops_the_write(tmp_path):
    # check_warehouse checks this promise with a stand-in; here the tracker
    # is one built as a user would, before its run is complete.
    with pytest.raises(ValueError, match="hasn't finished a run"):
        SqliteWarehouse(tmp_path / "shop.db").write(
            "items", [{"id": 1}], key="id", deleted_by=NewIds()
        )
