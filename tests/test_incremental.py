"""Incremental: fetching only what changed since the last run, without losing any.

The promises, in order: what a run asks the API for, which records come
through, when the state moves on (only after a whole run), how values
compare, and the settings for APIs that differ (body filters, no filter,
other forms, records without the field). changes_api is the fake API
throughout; its filter is checked here too, as the tests depend on it.
"""

import json
import logging

import httpx2
import pytest

from api_explorer import ApiError, Incremental, Keyset, PageRequest, logging_to
from api_explorer.testing import FakeApi, changes_api, keyset_api, replies


def at(day: int, hour: int = 0) -> str:
    return f"2026-01-{day:02d}T{hour:02d}:00:00Z"


def rec(id_: int, day: int, hour: int = 0) -> dict:
    return {"id": id_, "updated_at": at(day, hour)}


def ids(records) -> list[int]:
    return [r["id"] for r in records]


def run(api, since: Incremental, **options) -> list[int]:
    return ids(api.client().records("/orders/", incremental=since, **options))


def orders() -> Incremental:
    return Incremental("updated_at", "updated_since", key="id")


# What a run asks for, and what comes through


def test_a_first_run_fetches_everything_and_remembers_the_newest():
    data = [rec(1, 1), rec(2, 3), rec(3, 2)]
    api = changes_api(data)
    since = orders()
    assert run(api, since) == [1, 2, 3]
    assert api.queries == [{}]
    assert since.state == {"last": at(3), "seen": ["2"]}


def test_the_next_run_asks_from_the_last_value_and_gets_only_changes():
    data = [rec(1, 1), rec(2, 2)]
    api = changes_api(data)
    first = orders()
    run(api, first)
    data.append(rec(3, 4))
    data[0] = rec(1, 5)  # an edit moves a record's updated_at on
    second = Incremental("updated_at", "updated_since", key="id", state=first.state)
    assert run(api, second) == [1, 3]
    assert api.queries[-1] == {"updated_since": at(2)}
    assert second.state == {"last": at(5), "seen": ["1"]}


def test_a_record_added_at_the_last_value_is_not_lost():
    # The reason Incremental asks from the last value itself: record 3
    # shares record 2's time but arrived after the first run.
    data = [rec(1, 1), rec(2, 2)]
    api = changes_api(data)
    first = orders()
    run(api, first)
    data.append(rec(3, 2))
    second = Incremental("updated_at", "updated_since", key="id", state=first.state)
    assert run(api, second) == [3]
    assert second.state == {"last": at(2), "seen": ["2", "3"]}


def test_a_run_with_nothing_new_yields_nothing_and_keeps_its_place():
    api = changes_api([rec(1, 1), rec(2, 2)])
    first = orders()
    run(api, first)
    second = Incremental("updated_at", "updated_since", key="id", state=first.state)
    assert run(api, second) == []
    assert second.state == first.state


def test_records_in_any_order_are_tracked_by_the_highest_value():
    since = orders()
    assert run(changes_api([rec(3, 3), rec(1, 1), rec(2, 2)]), since) == [3, 1, 2]
    assert since.state["last"] == at(3)


def test_a_start_value_limits_the_first_run():
    api = changes_api([rec(1, 1), rec(2, 2), rec(3, 3)])
    since = Incremental("updated_at", "updated_since", key="id", start=at(2))
    assert run(api, since) == [2, 3]
    assert api.queries == [{"updated_since": at(2)}]


# When the state moves on


def test_a_run_that_fails_part_way_keeps_the_old_state():
    # Pages come newest first here, so the newest value seen before the
    # failure is ahead of the records never fetched. Saving it would skip
    # them for good.
    pages = {"1": {"results": [rec(9, 9)], "next": "?page=2"}}

    def answer(request):
        page = request.url.params.get("page", "1")
        return (
            httpx2.Response(200, json=pages[page])
            if page in pages
            else httpx2.Response(500)
        )

    since = orders()
    seen = []
    records = FakeApi(answer).client().records("/orders/", incremental=since)
    with pytest.raises(ApiError, match="500"):
        seen.extend(records)
    assert ids(seen) == [9]
    assert since.state == {"last": None, "seen": []}


def test_a_loop_that_stops_early_keeps_the_old_state():
    since = Incremental("updated_at", key="id", start=at(1))
    stream = since.track([rec(1, 2), rec(2, 3)])
    next(stream)
    stream.close()
    assert since.state == {"last": at(1), "seen": []}


def test_the_state_survives_a_round_trip_through_json():
    since = orders()
    run(changes_api([rec(1, 1), rec(2, 1)]), since)
    saved = json.loads(json.dumps(since.state))
    again = Incremental("updated_at", "updated_since", key="id", state=saved)
    assert again.last == at(1)
    assert list(again.track([rec(1, 1), rec(2, 1), rec(3, 1)])) == [rec(3, 1)]


# How values compare


@pytest.mark.parametrize(
    ("last", "value", "kept"),
    [
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00", False),
        ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00+01:00", False),
        ("2026-01-01", "2025-12-31T23:00:00Z", False),
        ("2026-01-01", "2026-01-01T00:00:01Z", True),
        (1_700_000_000, 1_700_000_001, True),
        (1_700_000_000, 1_699_999_999, False),
        ("b", "a", False),
    ],
    ids=[
        "Z-and-offset-agree",
        "other-zone-same-time",
        "no-offset-counts-as-UTC",
        "a-date-starts-the-day",
        "epoch-newer",
        "epoch-older",
        "plain-text",
    ],
)
def test_values_compare_as_times_numbers_or_text(last, value, kept):
    # The record was seen at the last value, so a value equal to it is
    # passed over, and only a greater one comes through.
    since = Incremental("t", key="id", state={"last": last, "seen": ["1"]})
    record = {"id": 1, "t": value}
    assert list(since.track([record])) == ([record] if kept else [])


def test_values_that_dont_compare_stop_the_run():
    since = Incremental("t", start=1_700_000_000)
    with pytest.raises(ApiError, match="don't compare"):
        list(since.track([{"t": "2026-01-01T00:00:00Z"}]))


@pytest.mark.parametrize("start", [True, {"a": 1}, [1]], ids=["bool", "dict", "list"])
def test_a_start_that_cant_be_ordered_is_refused_at_once(start):
    with pytest.raises(ApiError, match="can't be ordered"):
        Incremental("t", start=start)


def test_without_a_key_a_record_is_recognised_by_its_content():
    since = Incremental("t", state={"last": 5, "seen": []})
    run_one = list(since.track([{"t": 5, "name": "a"}]))
    again = Incremental("t", state=since.state)
    edited = {"t": 5, "name": "b"}
    assert list(again.track([{"t": 5, "name": "a"}, edited])) == [edited]
    assert run_one == [{"t": 5, "name": "a"}]


def test_a_record_changed_back_at_the_same_time_comes_through():
    # Regression: the list of records seen at the last value kept growing
    # across runs, so a record edited back to an earlier version within
    # the same instant matched that old version and was passed over. Now
    # the state holds only what the last run saw there.
    first = Incremental("t", state={"last": 5, "seen": []})
    list(first.track([{"id": 1, "t": 5, "v": None}]))
    second = Incremental("t", state=first.state)
    list(second.track([{"id": 1, "t": 5, "v": 0}]))
    third = Incremental("t", state=second.state)
    reverted = {"id": 1, "t": 5, "v": None}
    assert list(third.track([reverted])) == [reverted]


def test_with_a_key_an_edit_at_the_last_value_is_passed_over():
    # The trade-off key= makes, shown on purpose: record 1 changes again in
    # the same instant as the last run's newest value, and only content
    # recognition (the default) sees the change.
    state = {"last": 5, "seen": []}
    by_key = Incremental("t", key="id", state=state)
    by_content = Incremental("t", state=state)
    list(by_key.track([{"id": 1, "t": 5, "v": "a"}]))
    list(by_content.track([{"id": 1, "t": 5, "v": "a"}]))
    edited = {"id": 1, "t": 5, "v": "b"}
    assert list(Incremental("t", key="id", state=by_key.state).track([edited])) == []
    assert list(Incremental("t", state=by_content.state).track([edited])) == [edited]


def test_page_numbers_over_data_that_shifts_mid_run_can_lose_a_record():
    # The trap the docstring warns about, shown on purpose: record 1 is
    # edited between pages 1 and 2, so it moves to the end, the records
    # after it shift forward, and page 2 skips record 3. The run ends past
    # record 3's time, so the next run doesn't ask for it either.
    data = [rec(i, i) for i in range(1, 6)]
    api = changes_api(data, page_size=2)
    requests = 0

    def answer(request):
        nonlocal requests
        requests += 1
        if requests == 2:
            data[:] = [*data[1:], rec(1, 9)]
        return api(request)

    first = orders()
    assert run(FakeApi(answer), first) == [1, 2, 4, 5, 1]
    second = Incremental("updated_at", "updated_since", key="id", state=first.state)
    assert run(FakeApi(answer), second) == []


def test_a_tracked_run_cant_be_cut_short_by_max_pages():
    # Regression: records(max_pages=1) ends like a complete run, so the
    # tracker moved past the pages it never fetched.
    since = orders()
    api = changes_api([rec(i, i) for i in range(1, 6)], page_size=2)
    with pytest.raises(ValueError, match="max_pages can't be used with incremental"):
        list(api.client().records("/orders/", incremental=since, max_pages=1))
    assert api.calls == []
    assert since.state == {"last": None, "seen": []}


# Settings for APIs that differ


def test_a_nested_field_and_key_are_paths():
    since = Incremental("meta.changed", key="ref.id")
    records = [{"meta": {"changed": 3}, "ref": {"id": "x"}}]
    assert list(since.track(records)) == records
    assert since.state == {"last": 3, "seen": ['"x"']}


def test_without_a_param_nothing_is_sent_and_old_records_are_dropped_here():
    data = [rec(1, 1), rec(2, 2), rec(3, 3)]
    api = changes_api(data, compare=None)
    since = Incremental("updated_at", key="id", state={"last": at(2), "seen": ["2"]})
    assert run(api, since) == [3]
    assert api.queries == [{}]


def test_an_api_that_ignores_the_filter_still_gives_only_changes():
    api = changes_api([rec(1, 1), rec(2, 2), rec(3, 3)], compare=None)
    since = Incremental(
        "updated_at", "updated_since", key="id", state={"last": at(2), "seen": ["2"]}
    )
    assert run(api, since) == [3]


def test_the_filter_can_go_in_a_post_body():
    api = replies(httpx2.Response(200, json={"results": [], "next": None}))
    since = Incremental("updated_at", "since", send_in="body", start=at(1))
    run(api, since, method="POST", json={"query": "late"}, retry=True)
    assert api.bodies == [{"query": "late", "since": at(1)}]


def test_a_body_filter_needs_a_json_object():
    since = Incremental("updated_at", "since", send_in="body", start=at(1))
    with pytest.raises(ApiError, match="isn't a JSON object"):
        since.apply(PageRequest("http://h/x", "POST", [1, 2]))


def test_send_as_gives_the_value_in_the_form_the_api_filters_by():
    api = changes_api([])
    since = Incremental(
        "updated_at", "date_from", start=at(2, 15), send_as=lambda value: value[:10]
    )
    run(api, since)
    assert api.queries == [{"date_from": "2026-01-02"}]


def test_a_record_without_the_field_stops_the_run_by_default():
    since = orders()
    with pytest.raises(ApiError, match="no 'updated_at'"):
        list(since.track([rec(1, 1), {"id": 2}]))
    assert since.state == {"last": None, "seen": []}


@pytest.mark.parametrize(
    ("missing", "expected"), [("keep", [1, 2, 3]), ("drop", [1, 3])]
)
def test_records_without_the_field_can_be_kept_or_dropped(missing, expected):
    since = Incremental("updated_at", key="id", missing=missing)
    records = [rec(1, 1), {"id": 2, "updated_at": None}, rec(3, 2)]
    assert ids(since.track(records)) == expected
    assert since.state == {"last": at(2), "seen": ["3"]}


@pytest.mark.parametrize(
    ("option", "value"), [("send_in", "header"), ("missing", "skip")]
)
def test_unknown_settings_are_refused(option, value):
    with pytest.raises(ValueError, match=option):
        Incremental("t", **{option: value})


# How it fits with the rest of records()


def test_a_model_sees_only_the_records_that_come_through():
    data = [rec(1, 1), rec(2, 2)]
    since = Incremental("updated_at", key="id", state={"last": at(2), "seen": ["2"]})
    data.append(rec(3, 3))
    got = list(changes_api(data).client().records("/o/", incremental=since, model=dict))
    assert ids(got) == [3]


def test_a_finished_run_logs_what_it_kept_and_where_the_next_starts():
    kept = []

    class Keep(logging.Handler):
        def emit(self, record):
            kept.append(record)

    since = Incremental("updated_at", key="id", state={"last": at(1), "seen": ["1"]})
    api = changes_api([rec(1, 1), rec(2, 2)])
    with logging_to(Keep(), "DEBUG"):
        list(api.client(name="shop").records("/orders/", incremental=since))
    [event] = [r for r in kept if getattr(r, "event", None) == "incremental"]
    assert event.data == {"kept": 1, "dropped": 1, "last": at(2)}
    assert event.client == "shop"


@pytest.mark.parametrize(
    ("compare", "since", "expected"),
    [(">=", at(2), [2, 3]), (">", at(2), [3]), (None, at(2), [1, 2, 3])],
    ids=["from", "after", "no-filter"],
)
def test_changes_api_filters_as_told(compare, since, expected):
    api = changes_api([rec(1, 1), rec(2, 2), rec(3, 3)], compare=compare)
    assert ids(api.client().records(f"/o/?updated_since={since}")) == expected


# Keyset paging


def keyset(**options) -> Keyset:
    return Keyset("updated_at", "updated_since", **{"limit": 2, **options})


def keyset_run(api, since=None, **options) -> list[int]:
    since = since or orders()
    return run(api, since, paging=keyset(**options))


def test_keyset_pages_from_each_page_s_last_value():
    api = keyset_api([rec(i, i) for i in range(1, 6)])
    assert keyset_run(api) == [1, 2, 3, 4, 5]
    assert api.queries == [
        {"limit": "2"},
        {"limit": "2", "updated_since": at(2)},
        {"limit": "2", "updated_since": at(3)},
        {"limit": "2", "updated_since": at(4)},
        {"limit": "2", "updated_since": at(5)},
    ]


def test_keyset_starts_from_the_incremental_s_last_value():
    api = keyset_api([rec(i, i) for i in range(1, 6)])
    since = Incremental("updated_at", "updated_since", key="id", start=at(4))
    assert keyset_run(api, since) == [4, 5]
    assert api.queries[0] == {"updated_since": at(4), "limit": "2"}


def test_keyset_gets_records_sharing_a_value_across_a_page_boundary():
    # Page 1 ends inside the run of 2s; page 2 starts from 2 and holds
    # them all, with room to spare, so it moves on to 3.
    data = [rec(1, 1), rec(2, 2), rec(3, 2), rec(4, 2), rec(5, 3)]
    assert keyset_run(keyset_api(data), limit=4) == [1, 2, 3, 4, 5]


def test_keyset_refuses_more_records_at_one_value_than_a_page_holds():
    data = [rec(1, 1), rec(2, 2), rec(3, 2), rec(4, 2)]
    with pytest.raises(ApiError, match="share updated_at=.*raise limit"):
        keyset_run(keyset_api(data))


def test_keyset_notices_a_server_that_caps_the_page_size():
    data = [rec(i, i) for i in range(1, 8)]
    assert keyset_run(keyset_api(data, cap=2), limit=5) == [1, 2, 3, 4, 5, 6, 7]


def test_keyset_loses_nothing_when_the_data_changes_during_a_run():
    # The case page numbers lose a record in (see the test above): record 1
    # is edited between pages, and moves to the end, where Keyset finds it.
    data = [rec(i, i) for i in range(1, 6)]
    api = keyset_api(data)
    requests = 0

    def answer(request):
        nonlocal requests
        requests += 1
        if requests == 2:
            data[0] = rec(1, 9)
        return api(request)

    first = orders()
    assert keyset_run(FakeApi(answer), first) == [1, 2, 3, 4, 5, 1]
    second = Incremental("updated_at", "updated_since", key="id", state=first.state)
    assert keyset_run(FakeApi(answer), second) == []


def test_keyset_needs_pages_sorted_by_its_field():
    with pytest.raises(ApiError, match="isn't sorted by 'updated_at'"):
        keyset_run(
            replies(httpx2.Response(200, json={"results": [rec(1, 2), rec(2, 1)]}))
        )


def test_keyset_notices_an_api_that_ignores_its_filter():
    api = keyset_api([rec(i, i) for i in range(1, 6)], ignore_filter=True)
    with pytest.raises(ApiError, match="ignores 'updated_since'"):
        keyset_run(api)


def test_keyset_needs_every_record_to_have_its_field():
    body = {"results": [rec(1, 1), {"id": 2}]}
    with pytest.raises(ApiError, match="no 'updated_at', so Keyset"):
        keyset_run(
            replies(httpx2.Response(200, json=body)),
            Incremental("updated_at", missing="keep"),
        )


def test_keyset_can_send_its_parameters_in_a_post_body():
    pages = [{"results": [rec(1, 1), rec(2, 2)]}, {"results": [rec(2, 2)]}]
    api = replies(*(httpx2.Response(200, json=page) for page in pages))
    records = api.client().records(
        "/search/",
        method="POST",
        json={"q": "x"},
        retry=True,
        incremental=orders(),
        paging=keyset(send_in="body"),
    )
    assert ids(records) == [1, 2]
    assert api.bodies == [
        {"q": "x", "limit": 2},
        {"q": "x", "limit": 2, "updated_since": at(2)},
    ]


def test_keyset_on_its_own_yields_the_repeats_at_each_boundary():
    # Shown on purpose: the overlap is why Keyset pairs with a tracker.
    api = keyset_api([rec(i, i) for i in range(1, 4)])
    records = api.client().records("/o/", paging=keyset())
    assert ids(records) == [1, 2, 2, 3, 3]


@pytest.mark.parametrize(
    ("option", "value"), [("send_in", "header"), ("limit", 1)], ids=["send_in", "limit"]
)
def test_keyset_refuses_unknown_settings(option, value):
    with pytest.raises(ValueError, match=option):
        Keyset("updated_at", "updated_since", **{option: value})


def test_incremental_passes_over_a_record_it_already_passed_this_run():
    since = Incremental("updated_at", key="id")
    records = [rec(1, 1), rec(2, 2), rec(3, 2), rec(2, 2)]
    assert ids(since.track(records)) == [1, 2, 3]
