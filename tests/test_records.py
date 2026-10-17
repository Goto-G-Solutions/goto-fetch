"""select, flatten, explode and parse_datetime: reshaping lists of records."""

from datetime import UTC, datetime

import pytest

from api_explorer import explode, flatten, parse_datetime, select


def test_select_names_each_column_by_its_path():
    records = [{"id": 1, "user": {"name": "Ann"}}, {"id": 2, "user": {"name": "Bo"}}]
    assert select(records, ["id", "user.name"]) == [
        {"id": 1, "user.name": "Ann"},
        {"id": 2, "user.name": "Bo"},
    ]


def test_select_passes_each_path_to_get_path():
    # get_path's rules are tested in test_paths; this checks select uses
    # them, with fallbacks, wildcards and missing fields in one go.
    records = [{"url": "a", "tags": [{"n": "x"}]}, {"download_url": "b"}]
    assert select(records, ["url|download_url", "tags[*].n", "missing"]) == [
        {"url|download_url": "a", "tags[*].n": ["x"], "missing": None},
        {"url|download_url": "b", "tags[*].n": [], "missing": None},
    ]


def test_select_lets_an_extractor_compute_a_column():
    records = [{"first": "Ann", "last": "Lee"}]
    rows = select(records, ["full"], {"full": lambda r: f"{r['first']} {r['last']}"})
    assert rows == [{"full": "Ann Lee"}]


def test_flatten_names_nested_values_by_path():
    record = {"id": 1, "user": {"name": "Ann"}, "tags": ["a", "b"]}
    assert flatten(record) == {
        "id": 1,
        "user.name": "Ann",
        "tags[0]": "a",
        "tags[1]": "b",
    }


def test_flatten_keeps_empty_containers_as_values():
    assert flatten({"meta": {}, "tags": []}) == {"meta": {}, "tags": []}


def test_flatten_stops_at_max_depth():
    record = {"a": {"b": {"c": 1}}}
    assert flatten(record, max_depth=1) == {"a": {"b": {"c": 1}}}
    assert flatten(record, max_depth=2) == {"a.b": {"c": 1}}


def test_explode_steps_through_lists_together_padding_each_record_to_its_longest():
    records = [
        {"id": 1, "tags": ["a", "b"], "scores": [10]},
        {"id": 2, "tags": ["c"], "scores": [30]},
    ]
    # Padding is per record: record 2 gives one row, not two.
    assert explode(records) == [
        {"id": 1, "tags": "a", "scores": 10},
        {"id": 1, "tags": "b", "scores": None},
        {"id": 2, "tags": "c", "scores": 30},
    ]


def test_explode_forward_fill_repeats_the_last_value():
    records = [{"tags": ["a", "b"], "scores": [70]}]
    assert [r["scores"] for r in explode(records, fill="forward")] == [70, 70]


def test_explode_keeps_a_record_whose_lists_are_all_empty():
    assert explode([{"id": 3, "tags": []}]) == [{"id": 3, "tags": None}]


def test_explode_treats_none_as_empty_and_a_scalar_as_one_element():
    records = [{"id": 1, "tags": None}, {"id": 2, "tags": "solo"}]
    assert explode(records, columns=["tags"]) == [
        {"id": 1, "tags": None},
        {"id": 2, "tags": "solo"},
    ]


def test_explode_leaves_list_columns_it_wasnt_given_whole():
    records = [{"tags": ["a", "b"], "scores": [1, 2]}]
    assert explode(records, columns=["tags"]) == [
        {"tags": "a", "scores": [1, 2]},
        {"tags": "b", "scores": [1, 2]},
    ]


def test_explode_without_list_columns_returns_the_same_rows():
    records = [{"id": 1}, {"id": 2}]
    assert explode(records) == records


def test_explode_refuses_an_unknown_fill():
    with pytest.raises(ValueError, match="fill must be"):
        explode([{"items": ["a"]}], fill="invalid")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024-01-02T10:00:00Z", datetime(2024, 1, 2, 10, tzinfo=UTC)),
        ("2024-01-02 10:00:00", datetime(2024, 1, 2, 10)),
        ("2024-01-02", datetime(2024, 1, 2)),
    ],
)
def test_parse_datetime_reads_iso_8601(text, expected):
    assert parse_datetime(text) == expected


@pytest.mark.parametrize("value", ["02/01/2024", None, 20240102])
def test_parse_datetime_gives_none_for_what_it_cannot_read(value):
    assert parse_datetime(value) is None


def test_parse_datetime_passes_a_datetime_through():
    moment = datetime(2024, 1, 2)
    assert parse_datetime(moment) is moment
