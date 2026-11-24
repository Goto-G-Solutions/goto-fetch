"""Changes: passing on only new and changed records, and noticing deleted ones.

The promises: what comes through on later runs, what a fingerprint
ignores, when the state and `deleted` move on (only after a whole run),
and how it fits records() and tracked_run. A property test in
test_properties checks the same rules over many random runs.
"""

import json

import pytest
from conftest import Collect

from goto_fetch import (
    ApiError,
    Changes,
    logging_to,
    tracked_run,
)
from goto_fetch.testing import MemoryState, next_link_api


def product(id_, price=10, **extra) -> dict:
    return {"id": id_, "price": price, **extra}


def again(changes: Changes, **options) -> Changes:
    """The next run's Changes, from this run's state."""
    return Changes(changes.key, state=changes.state, **options)


def test_a_first_run_passes_everything_on_and_nothing_is_deleted():
    changes = Changes("id")
    records = [product(1), product(2)]
    assert list(changes.track(records)) == records
    assert sorted(changes.state["hashes"]) == ["1", "2"]
    assert changes.deleted == []


def test_a_later_run_passes_on_only_new_and_changed_records():
    first = Changes("id")
    list(first.track([product(1), product(2), product(3)]))
    second = again(first)
    got = list(second.track([product(1), product(2, price=12), product(4)]))
    assert got == [product(2, price=12), product(4)]
    assert second.deleted == [3]


def test_a_run_with_nothing_changed_passes_nothing_on():
    first = Changes("id")
    list(first.track([product(1)]))
    second = again(first)
    assert list(second.track([product(1)])) == []
    assert second.state == first.state


def test_the_order_of_keys_in_a_record_doesnt_count_as_a_change():
    first = Changes("id")
    list(first.track([{"id": 1, "a": 1, "b": 2}]))
    assert list(again(first).track([{"b": 2, "a": 1, "id": 1}])) == []


def test_keys_of_different_types_are_different_records():
    first = Changes("id")
    list(first.track([product(1)]))
    second = again(first, max_deleted=1.0)
    assert list(second.track([product("1")])) == [product("1")]
    assert second.deleted == [1]


@pytest.mark.parametrize(
    ("ignore", "before", "after"),
    [
        (["fetched_at"], product(1, fetched_at="t1"), product(1, fetched_at="t2")),
        (
            ["stats.views"],
            product(1, stats={"views": 1}),
            product(1, stats={"views": 9}),
        ),
        (
            ["items[*].stock"],
            product(1, items=[{"sku": "a", "stock": 1}]),
            product(1, items=[{"sku": "a", "stock": 0}]),
        ),
        # Regression: an index in an ignore path was walked as a dict key,
        # so it ignored nothing.
        (
            ["items[0].stock"],
            product(1, items=[{"sku": "a", "stock": 1}, {"sku": "b", "stock": 1}]),
            product(1, items=[{"sku": "a", "stock": 0}, {"sku": "b", "stock": 1}]),
        ),
        (
            ["items[-1]"],
            product(1, items=[{"sku": "a"}, {"sku": "b"}]),
            product(1, items=[{"sku": "a"}, {"sku": "c"}]),
        ),
    ],
    ids=["top-level", "nested", "in-a-list", "one-item", "last-item"],
)
def test_ignored_fields_dont_count_as_changes_but_others_do(ignore, before, after):
    first = Changes("id", ignore=ignore)
    list(first.track([before]))
    assert list(again(first, ignore=ignore).track([after])) == []
    priced = {**after, "price": 99}
    assert list(again(first, ignore=ignore).track([priced])) == [priced]


def test_a_fingerprint_of_your_own_decides_what_counts_as_a_change():
    # An API that gives each record a version: only a new version counts.
    def by_version(record):
        return str(record["version"])

    first = Changes("id", fingerprint=by_version)
    list(first.track([{"id": 1, "version": 1, "seen_at": "t1"}]))
    second = Changes("id", fingerprint=by_version, state=first.state)
    assert list(second.track([{"id": 1, "version": 1, "seen_at": "t2"}])) == []
    third = Changes("id", fingerprint=by_version, state=first.state)
    bumped = {"id": 1, "version": 2, "seen_at": "t2"}
    assert list(third.track([bumped])) == [bumped]


def test_ignore_and_a_fingerprint_together_are_refused():
    with pytest.raises(ValueError, match="ignore or fingerprint, not both"):
        Changes("id", ignore=["seen_at"], fingerprint=str)


def test_a_fingerprint_must_give_text():
    changes = Changes("id", fingerprint=lambda record: record["version"])
    with pytest.raises(TypeError, match="must return text, got int"):
        list(changes.track([{"id": 1, "version": 1}]))


def test_an_ignore_path_that_leads_nowhere_changes_nothing():
    first = Changes("id", ignore=["missing.deeper", "price.cents", "id[0]"])
    list(first.track([product(1)]))
    assert list(again(first, ignore=["missing.deeper"]).track([product(1)])) == []


def test_a_record_seen_twice_in_one_run_comes_through_once_unless_it_changed():
    changes = Changes("id")
    records = [product(1), product(1), product(2), product(2, price=5)]
    assert list(changes.track(records)) == [product(1), product(2), product(2, price=5)]


def test_a_run_that_stops_early_keeps_the_old_state_and_knows_no_deletions():
    first = Changes("id")
    list(first.track([product(1), product(2)]))
    second = again(first)
    stream = second.track([product(1, price=3), product(2)])
    next(stream)
    stream.close()
    assert second.state == first.state
    assert second.deleted is None


def test_a_run_that_saw_none_of_the_last_runs_records_is_refused():
    # Regression: an API answering a fault with an empty page looked like a
    # complete run, so `deleted` listed every record and a warehouse write
    # removed every row (issue #11).
    first = Changes("id")
    list(first.track([product(1), product(2)]))
    second = again(first)
    with pytest.raises(ApiError, match="saw none of the 2 records"):
        list(second.track([]))
    assert second.deleted is None
    assert second.state == first.state


def test_max_deleted_of_all_lets_every_record_go():
    first = Changes("id")
    list(first.track([product(1), product(2)]))
    second = again(first, max_deleted=1.0)
    assert list(second.track([])) == []
    assert second.deleted == [1, 2]
    assert second.state == {"hashes": {}}


@pytest.mark.parametrize(
    ("max_deleted", "kept", "allowed"),
    [(1, 3, True), (1, 2, False), (0.5, 2, True), (0.5, 1, False), (0, 4, True)],
    ids=["count-met", "count-passed", "share-met", "share-passed", "none-gone"],
)
def test_max_deleted_is_a_count_or_a_share_of_the_last_run(max_deleted, kept, allowed):
    first = Changes("id")
    list(first.track([product(i) for i in range(4)]))
    second = again(first, max_deleted=max_deleted)
    run = second.track([product(i) for i in range(kept)])
    if allowed:
        assert list(run) == []
        assert second.deleted == list(range(kept, 4))
    else:
        with pytest.raises(ApiError, match=f"{4 - kept} of the 4 records"):
            list(run)
        assert second.deleted is None


def test_a_first_run_with_no_records_is_not_refused():
    # Nothing was held before, so nothing can be wrongly called deleted.
    changes = Changes("id")
    assert list(changes.track([])) == []
    assert changes.deleted == []


@pytest.mark.parametrize("bad", [-1, 1.5, -0.1, True, "half"])
def test_max_deleted_takes_only_a_count_or_a_share(bad):
    with pytest.raises(ValueError, match="max_deleted must be a count"):
        Changes("id", max_deleted=bad)


def test_a_record_without_the_key_stops_the_run():
    changes = Changes("id")
    with pytest.raises(ApiError, match="no key 'id'"):
        list(changes.track([product(1), {"price": 3}]))
    assert changes.state == {"hashes": {}}


def test_a_changes_state_survives_a_round_trip_through_json():
    first = Changes("id")
    list(first.track([product(1), product(2)]))
    saved = json.loads(json.dumps(first.state))
    assert list(Changes("id", state=saved).track([product(1), product(2)])) == []


# How it fits with the rest


def test_records_with_changes_fetch_everything_and_deliver_the_changes():
    store = MemoryState()
    data = [product(i) for i in range(5)]
    with tracked_run(store, "products", Changes, key="id") as changes:
        list(
            next_link_api(data, page_size=2)
            .client()
            .records("/p/", incremental=changes)
        )
    data[1] = product(1, price=50)
    del data[4]
    api = next_link_api(data, page_size=2)
    with tracked_run(store, "products", Changes, key="id") as changes:
        got = list(api.client().records("/p/", incremental=changes))
    assert got == [product(1, price=50)]
    assert changes.deleted == [4]
    assert api.queries == [{}, {"page": "2"}]  # nothing added to the request


def test_a_finished_run_logs_what_changed():
    collect = Collect()
    first = Changes("id")
    list(first.track([product(1), product(2), product(3)]))
    with logging_to(collect, "DEBUG"):
        list(again(first).track([product(1), product(2, price=1), product(4)]))
    [event] = [r for r in collect.records if getattr(r, "event", None) == "changes"]
    assert event.data == {"new": 1, "changed": 1, "unchanged": 1, "deleted": 1}
