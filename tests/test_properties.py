"""Property-based tests: rules that must hold for any input, not just chosen examples.

Hypothesis generates messy JSON, odd paths and awkward servers, and tries
to break each rule. These complement the example tests in the other files;
when one fails, Hypothesis prints the smallest input that breaks it, and a
regression test for it belongs with the example tests.
"""

import json

import httpx2
import pytest

pytest.importorskip("hypothesis", reason="property tests need the dev dependencies")

from conftest import BACKENDS
from hypothesis import HealthCheck, given, settings
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
from api_explorer.testing import offset_api

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


# 200 examples: at 60, this missed ints beside floats in a list for a while.
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
    count=st.sampled_from(["actual", None]),
)
def test_offset_paging_returns_every_record_once_whatever_the_server_does(
    total, limit, cap, count
):
    handler = offset_api(total, cap=cap, count=count)
    client = Client(
        "http://api.test/api",
        transport=httpx2.MockTransport(handler),
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
        return httpx2.Response(
            200, json={"hits": pages[index], "next_cursor": following}
        )

    client = Client(
        "http://api.test/api",
        transport=httpx2.MockTransport(handler),
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
