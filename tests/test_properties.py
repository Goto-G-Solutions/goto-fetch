"""Property-based tests: rules that must hold for any input, not just chosen examples.

Hypothesis generates messy JSON, odd paths and awkward servers, and tries
to break each rule. These complement the example tests in the other files;
when one fails, Hypothesis prints the smallest input that breaks it, and a
regression test for it belongs with the example tests.
"""

import json

import pytest

from api_explorer import http

pytest.importorskip("hypothesis", reason="property tests need the dev dependencies")

from conftest import BACKENDS
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from api_explorer import (
    Client,
    Offset,
    Retry,
    explode,
    flatten,
    get_path,
    parse_path,
    select,
    to_frame,
)
from api_explorer.testing import FakeApi, offset_api

# JSON as APIs send it: no NaN or infinity, which JSON can't carry.
SCALARS = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=8)
)
KEYS = st.sampled_from(["a", "b", "c", "id", "0", "1"])
JSON = st.recursive(
    SCALARS,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(KEYS, inner, max_size=4)
    ),
    max_leaves=12,
)
RECORDS = st.lists(st.dictionaries(KEYS, JSON, max_size=4), max_size=8)
SEGMENTS = st.lists(
    st.sampled_from(["a", "b", "c", "id", "0", "1", "-1", "*"]), min_size=1, max_size=4
)


# Any text as a key, except "*", which paths always read as the wildcard.
TEXT_KEYS = st.text(max_size=6).filter(lambda key: key != "*")


def path_of(segments: list[str]) -> str:
    return ".".join(segments)


@given(data=JSON, alternatives=st.lists(SEGMENTS, min_size=1, max_size=3))
def test_get_path_never_raises_on_any_json(data, alternatives):
    get_path(data, "|".join(path_of(s) for s in alternatives))


@given(
    keys=st.lists(
        st.text(min_size=1, max_size=10).filter(lambda k: k.strip() == k and k != "*"),
        min_size=1,
        max_size=4,
    )
)
def test_any_key_survives_quoting(keys):
    def quote(key: str) -> str:
        escaped = key.replace("\\", "\\\\").replace('"', '\\"')
        return f'["{escaped}"]'

    assert parse_path("".join(quote(k) for k in keys)) == keys


@given(
    record=st.dictionaries(
        TEXT_KEYS,
        st.recursive(
            SCALARS,
            lambda inner: st.dictionaries(TEXT_KEYS, inner, max_size=3),
            max_leaves=6,
        ),
        max_size=4,
    )
)
def test_every_flattened_key_reads_back_with_get_path(record):
    # Any keys at all, dots and quotes included: no value is lost or merged.
    for key, value in flatten(record).items():
        assert get_path(record, key) == value


@given(
    records=RECORDS, columns=st.lists(SEGMENTS.map(path_of), max_size=4, unique=True)
)
def test_select_gives_one_row_per_record_with_exactly_the_columns_asked_for(
    records, columns
):
    rows = select(records, columns)
    assert len(rows) == len(records)
    assert all(list(row) == columns for row in rows)


@given(record=st.dictionaries(KEYS, JSON, max_size=4))
def test_flatten_leaves_no_nesting_except_empty_containers(record):
    for value in flatten(record).values():
        assert not (isinstance(value, (dict, list)) and value)


@given(
    records=st.lists(
        st.dictionaries(
            st.sampled_from(["id", "tags", "scores"]),
            st.lists(st.integers(), max_size=4) | st.integers() | st.none(),
            max_size=3,
        ),
        max_size=6,
    )
)
def test_explode_gives_each_record_as_many_rows_as_its_longest_list(records):
    columns = ["tags", "scores"]

    def length(value):
        if value is None:
            return 0
        return len(value) if isinstance(value, list) else 1

    expected = sum(max(1, max(length(r.get(c)) for c in columns)) for r in records)
    assert len(explode(records, columns=columns)) == expected


# 200 examples: fewer can miss ints beside floats inside a list.
@settings(max_examples=200, deadline=None)
@given(records=RECORDS, backend=st.sampled_from(BACKENDS))
def test_to_frame_builds_a_frame_from_any_json_records(records, backend):
    frame = to_frame(records, backend)
    # A frame can't hold rows without columns, so records with no keys at
    # all give an empty frame (to_frame's docstring says so).
    has_columns = any(records)
    assert len(frame) == (len(records) if has_columns else 0)


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    total=st.integers(0, 300),
    limit=st.integers(1, 120),
    cap=st.none() | st.integers(1, 120),
    count=st.booleans(),
)
def test_offset_paging_returns_every_record_once_whatever_the_server_does(
    total, limit, cap, count
):
    handler = offset_api(total, cap=cap, count=count)
    client = Client(
        "http://api.test/api",
        transport=FakeApi(handler),
        retry=Retry(backoff=0),
    )
    records = list(
        client.records(
            "/items/",
            results_key="items",
            count_key="count" if count else None,
            paging=Offset(limit=limit),
        )
    )
    assert records == list(range(total))
    page = min(limit, cap) if cap else limit
    # At most one request beyond the pages the records need.
    assert len(handler.queries) <= -(-total // page) + 1


@given(sizes=st.lists(st.integers(0, 5), min_size=1, max_size=6))
def test_cursor_paging_by_post_body_returns_every_page_in_order(sizes):
    # Pages may be empty mid-run here: that is what stop_on_empty=False is for.
    from api_explorer import Cursor

    pages = [
        list(range(sum(sizes[:i]), sum(sizes[: i + 1]))) for i in range(len(sizes))
    ]

    def handler(request):
        index = int(json.loads(request.content).get("cursor") or 0)
        following = str(index + 1) if index + 1 < len(pages) else None
        return http.Response(200, json={"hits": pages[index], "next_cursor": following})

    client = Client(
        "http://api.test/api",
        transport=FakeApi(handler),
        retry=Retry(backoff=0),
    )
    records = client.records(
        "/search/",
        method="POST",
        json={},
        results_key="hits",
        paging=Cursor(send_in="body", stop_on_empty=False),
    )
    assert list(records) == list(range(sum(sizes)))


@settings(deadline=None)
@given(
    batches=st.lists(
        st.lists(st.tuples(st.integers(0, 5), st.integers(0, 1)), max_size=4),
        min_size=1,
        max_size=5,
    ),
    page_size=st.integers(1, 4),
)
def test_incremental_runs_never_lose_or_repeat_a_version_of_a_record(
    batches, page_size
):
    # Between runs, a batch adds or edits records: (id, step), where step
    # moves the clock on by 0 or 1 minute, so changes often share a time
    # with the last run's newest. Each run must yield every version it
    # hasn't yielded before, and none twice.
    from api_explorer import Incremental
    from api_explorer.testing import changes_api

    data: dict[int, dict] = {}
    rows: list[dict] = []
    api = changes_api(rows, page_size=page_size)
    clock, state, version = 0, None, 0
    yielded: list[tuple[int, int]] = []
    for batch in batches:
        for id_, step in batch:
            clock += step
            version += 1
            data[id_] = {
                "id": id_,
                "updated_at": f"2026-01-01T00:{clock:02d}:00Z",
                "v": version,
            }
        rows[:] = list(data.values())
        since = Incremental("updated_at", "updated_since", state=state)
        got = list(api.client().records("/o/", incremental=since))
        yielded += [(r["id"], r["v"]) for r in got]
        state = since.state
        latest = {(r["id"], r["v"]) for r in rows}
        assert latest <= set(yielded)
    assert len(yielded) == len(set(yielded))


@given(
    runs=st.lists(
        st.dictionaries(st.integers(0, 6), st.integers(0, 2), max_size=6),
        min_size=1,
        max_size=5,
    )
)
def test_changes_delivers_exactly_what_differs_from_the_last_run(runs):
    # Each run is the whole source: {id: value}. Changes must pass on the
    # records that are new or differ from the last run, and list as
    # deleted the ids the last run had and this one lacks.
    from api_explorer import Changes

    state, before = None, {}
    for source in runs:
        changes = Changes("id", state=state)
        records = [{"id": k, "v": v} for k, v in source.items()]
        got = list(changes.track(records))
        assert got == [r for r in records if before.get(r["id"]) != r["v"]]
        assert sorted(changes.deleted) == sorted(set(before) - set(source))
        state, before = changes.state, source


FAILURES = st.sampled_from(["ok", "ok", "ok", "503", "drop", "refused"])


def flaky_source(rows: list[dict], data) -> "Client":
    """A client for changes_api(rows) where each request may fail, as Hypothesis picks.

    A request answers normally, with a 503, or by a dropped or refused
    connection. Retries (3 tries, no waiting) rescue some failures; three
    in a row end the run with ApiError.
    """
    from api_explorer.testing import changes_api

    api = changes_api(rows, page_size=2)

    def answer(request):
        outcome = data.draw(FAILURES)
        if outcome == "503":
            return http.Response(503)
        if outcome == "drop":
            raise http.ConnectionDropped("connection dropped mid-response")
        if outcome == "refused":
            raise http.ConnectError("connection refused")
        return api(request)

    return FakeApi(answer).client(retry=Retry(attempts=3, backoff=0))


BATCHES = st.lists(
    st.lists(
        st.tuples(st.integers(0, 5), st.integers(0, 1), st.booleans()), max_size=4
    ),
    min_size=1,
    max_size=6,
)


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    batches=BATCHES, tracker=st.sampled_from(["incremental", "changes"]), data=st.data()
)
def test_a_warehouse_fed_by_tracked_runs_mirrors_the_source_despite_failures(
    batches, tracker, data
):
    # Between runs, a batch adds, edits or (for Changes, which can see it)
    # deletes records; the clock moves on by 0 or 1 minute per change, so
    # ties with the last run's newest value are common. Each run stores
    # what it delivers into a warehouse, inside tracked_run. After every
    # run that succeeds, the warehouse must equal the source; after every
    # run that fails, the saved state must be as it was. The Hypothesis
    # statistics (--hypothesis-show-statistics) show both kinds of run.
    from api_explorer import ApiError, Changes, Incremental, tracked_run
    from api_explorer.testing import MemoryState

    store = MemoryState()
    source: dict[int, dict] = {}
    warehouse: dict[int, dict] = {}
    rows: list[dict] = []
    clock = version = 0
    for batch in batches:
        for id_, step, delete in batch:
            clock += step
            version += 1
            if delete and tracker == "changes":
                source.pop(id_, None)
            else:
                stamp = f"2026-01-01T00:{clock:02d}:00Z"
                source[id_] = {"id": id_, "updated_at": stamp, "v": version}
        rows[:] = list(source.values())
        if tracker == "incremental":
            args, options = (Incremental, "updated_at", "updated_since"), {}
        else:
            args, options = (Changes,), {"key": "id"}
        before = store.get("orders")
        client = flaky_source(rows, data)
        try:
            with tracked_run(store, "orders", *args, **options) as run:
                if tracker == "incremental":
                    # Written as they arrive: a failed run leaves part of
                    # its records behind, and the next run must set them
                    # right.
                    for record in client.records("/orders/", incremental=run):
                        warehouse[record["id"]] = record
                else:
                    # All or nothing, as Changes.deleted needs: records a
                    # failed run stored would be unknown to the state, so a
                    # later deletion of one could never be listed.
                    delivered = list(client.records("/orders/", incremental=run))
                    for record in delivered:
                        warehouse[record["id"]] = record
                    for id_ in run.deleted or []:
                        warehouse.pop(id_, None)
        except ApiError:
            event("a run failed")
            assert store.get("orders") == before
            continue
        event("a run succeeded")
        assert warehouse == source


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    sizes=st.lists(st.integers(0, 4), min_size=1, max_size=5),
    limit=st.integers(2, 4),
    data=st.data(),
)
def test_keyset_runs_mirror_the_source_while_it_changes_mid_run(sizes, limit, data):
    # Each run first adds some records; then, before any request, a record
    # may be edited (it moves to the end, with a newer time) or the request
    # may fail. Keyset with Incremental must still leave the warehouse
    # equal to the source after every run that succeeds. A run fails when
    # retries run out, or when more records share a time than a page
    # holds (Keyset refuses those); then the state must not move.
    from api_explorer import ApiError, Incremental, Keyset, tracked_run
    from api_explorer.testing import MemoryState, keyset_api

    store = MemoryState()
    source: dict[int, dict] = {}
    warehouse: dict[int, dict] = {}
    rows: list[dict] = []
    clock = 0

    def stamp() -> str:
        nonlocal clock
        clock += data.draw(st.integers(0, 1))
        return f"2026-01-01T{clock // 60:02d}:{clock % 60:02d}:00Z"

    def sync() -> None:
        rows[:] = list(source.values())

    api = keyset_api(rows)

    def answer(request):
        if source and data.draw(st.booleans()):
            id_ = data.draw(st.sampled_from(sorted(source)))
            source[id_] = {
                **source[id_],
                "updated_at": stamp(),
                "edits": source[id_].get("edits", 0) + 1,
            }
            sync()
        outcome = data.draw(FAILURES)
        if outcome == "503":
            return http.Response(503)
        if outcome in ("drop", "refused"):
            raise http.ConnectError("connection lost")
        return api(request)

    for size in sizes:
        for _ in range(size):
            id_ = len(source)
            source[id_] = {"id": id_, "updated_at": stamp()}
        sync()
        before = store.get("orders")
        client = FakeApi(answer).client(retry=Retry(attempts=3, backoff=0))
        try:
            with tracked_run(
                store, "orders", Incremental, "updated_at", "updated_since"
            ) as run:
                for record in client.records(
                    "/orders/",
                    incremental=run,
                    paging=Keyset("updated_at", "updated_since", limit=limit),
                ):
                    warehouse[record["id"]] = record
        except ApiError:
            event("a run failed")
            assert store.get("orders") == before
            continue
        event("a run succeeded")
        assert warehouse == source


@settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(
    batches=st.lists(
        st.lists(
            st.tuples(
                st.integers(0, 5),
                st.integers(0, 1),
                st.booleans(),
                st.none() | st.integers(-3, 3) | st.text(max_size=3),
            ),
            max_size=4,
        ),
        min_size=1,
        max_size=4,
    ),
    tracker=st.sampled_from(["incremental", "changes"]),
    kind=st.sampled_from(["duckdb", "sqlite", "memory"]),
    data=st.data(),
)
def test_a_warehouse_mirrors_the_source_despite_failures(batches, tracker, kind, data):
    # The warehouse property again, with real warehouses (DuckDB, SQLite and
    # the in-memory one): write() inside tracked_run, through flaky
    # requests, with values that change kind between runs so columns widen. After every run that succeeds,
    # the table must hold the source exactly (compared as text, since a
    # widened column holds numbers as text); after a failed run, the
    # table and the state must be as they were.
    import tempfile
    from pathlib import Path

    from api_explorer import (
        ApiError,
        Changes,
        DuckDBWarehouse,
        Incremental,
        SqliteWarehouse,
        tracked_run,
    )
    from api_explorer.testing import MemoryWarehouse

    if kind == "duckdb":
        pytest.importorskip("duckdb")

    def text(value):
        return None if value is None else str(value)

    with tempfile.TemporaryDirectory() as folder:
        shop = {
            "duckdb": lambda: DuckDBWarehouse(Path(folder) / "shop.duckdb"),
            "sqlite": lambda: SqliteWarehouse(Path(folder) / "shop.db"),
            "memory": MemoryWarehouse,
        }[kind]()

        def table() -> dict:
            return {r["id"]: text(r.get("v")) for r in shop.read("orders")}

        source: dict[int, dict] = {}
        rows: list[dict] = []
        clock = 0
        for batch in batches:
            for id_, step, delete, value in batch:
                clock += step
                if delete and tracker == "changes":
                    source.pop(id_, None)
                else:
                    stamp = f"2026-01-01T00:{clock:02d}:00Z"
                    source[id_] = {"id": id_, "updated_at": stamp, "v": value}
            rows[:] = list(source.values())
            if tracker == "incremental":
                args, options = (Incremental, "updated_at", "updated_since"), {}
            else:
                args, options = (Changes,), {"key": "id"}
            # batch_size isn't part of Warehouse: only the database ones
            # take it, here so a run spans several batches.
            batches_of_two = {} if kind == "memory" else {"batch_size": 2}
            before_state, before_table = shop.get("orders"), table()
            client = flaky_source(rows, data)
            try:
                with tracked_run(shop, "orders", *args, **options) as run:
                    shop.write(
                        "orders",
                        client.records("/orders/", incremental=run),
                        key="id",
                        deleted_by=run if tracker == "changes" else None,
                        **batches_of_two,
                    )
            except ApiError:
                event("a run failed")
                assert shop.get("orders") == before_state
                assert table() == before_table
                continue
            event("a run succeeded")
            assert table() == {id_: text(r["v"]) for id_, r in source.items()}
