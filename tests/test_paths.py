"""get_path and parse_path: reading values out of nested JSON by path."""

import pytest

from api_explorer import get_path, parse_path


@pytest.mark.parametrize(
    ("path", "segments"),
    [
        ("user.name", ["user", "name"]),
        ("users[0].name", ["users", "0", "name"]),
        ("users.0.name", ["users", "0", "name"]),
        ("items[*].url", ["items", "*", "url"]),
        ("a[key].b", ["a", "key", "b"]),
        ('by_url["https://x.org/a.b"].hits', ["by_url", "https://x.org/a.b", "hits"]),
        ("by_url['x|y']", ["by_url", "x|y"]),
        ('a["say \\"hi\\""]', ["a", 'say "hi"']),
        ("[a.b]", ["a.b"]),
    ],
)
def test_parse_path_splits_on_dots_and_brackets_and_keeps_strings(path, segments):
    assert parse_path(path) == segments


@pytest.mark.parametrize("path", ['a["b', "a[b", "a]b", "a|b"])
def test_parse_path_refuses_what_it_cant_read(path):
    with pytest.raises(ValueError):
        parse_path(path)


def test_a_quoted_key_may_hold_a_line_break():
    # Regression, found by the property tests: quoted keys stopped at "\n".
    assert parse_path('["line\nbreak"]') == ["line\nbreak"]


def test_a_key_holding_path_characters_is_reached_in_quotes():
    # Regression: a key such as "a.b" couldn't be reached at all.
    data = {"by_url": {"https://x.org/a.b": 3, "x|y": 4}}
    assert get_path(data, 'by_url["https://x.org/a.b"]') == 3
    assert get_path(data, "missing|by_url['x|y']") == 4


def test_every_spelling_of_the_wildcard_falls_through_when_empty():
    # Regression: "items.*" collected like "[*]" but didn't fall through.
    data = {"items": [], "backup": [1]}
    assert get_path(data, "items.*|backup") == [1]


@pytest.mark.parametrize(
    ("data", "path", "value"),
    [
        ({"user": {"profile": {"age": 30}}}, "user.profile.age", 30),
        ({"users": [{"name": "A"}, {"name": "B"}]}, "users[1].name", "B"),
        ({"users": [{"name": "A"}, {"name": "B"}]}, "users.1.name", "B"),
        ({"items": [{"a": 1}, {"b": 2}]}, "items[-1].b", 2),
    ],
)
def test_get_path_walks_dict_keys_and_list_indices(data, path, value):
    assert get_path(data, path) == value


def test_a_digit_segment_is_a_key_in_a_dict():
    # Regression: digits used to become ints, so JSON keys like "2024"
    # (always strings) could never be reached.
    data = {"by_year": {"2024": {"count": 3}}, "rows": [{"0": "key"}]}
    assert get_path(data, "by_year.2024.count") == 3
    assert get_path(data, "rows.0.0") == "key"


def test_strings_are_not_walked():
    # Regression: "name.0" used to return the first letter of a string.
    assert get_path({"name": "abc"}, "name.0") is None
    assert get_path({"name": "abc"}, "name[*]") == []


@pytest.mark.parametrize(
    "path", ["missing", "user.missing", "items[10].a", "user.name.deeper"]
)
def test_a_path_that_isnt_there_gives_the_default(path):
    data = {"user": {"name": "Ann"}, "items": [{"a": 1}]}
    assert get_path(data, path) is None
    assert get_path(data, path, "fallback") == "fallback"


def test_none_data_gives_the_default():
    assert get_path(None, "any.path", "fallback") == "fallback"


def test_a_key_holding_none_counts_as_found():
    # The default is for absent keys only; an explicit None is an answer.
    assert get_path({"status": None}, "status", "fallback") is None
    assert get_path({"url": None, "backup": "b"}, "url|backup") is None


@pytest.mark.parametrize("value", ["", 0, False])
def test_a_falsy_value_counts_as_found_in_a_fallback(value):
    assert get_path({"a": value, "b": "other"}, "a|b") == value


def test_fallbacks_are_tried_in_order_until_one_exists():
    data = {"alternate": {"file_url": "f"}}
    assert get_path(data, "metadata.link|alternate.file_url") == "f"
    assert get_path(data, "m1|m2", "fallback") == "fallback"


def test_a_wildcard_collects_from_every_element_and_keeps_the_shape():
    data = {
        "items": [{"v": 1}, {}, {"v": 3}],
        "a": [{"b": [{"c": 1}, {"c": 2}]}, {"b": []}, {}],
    }
    # An element without the value contributes None, so results line up.
    assert get_path(data, "items[*].v") == [1, None, 3]
    assert get_path(data, "a[*].b[*].c") == [[1, 2], [], None]


@pytest.mark.parametrize("data", [{"items": None}, {"items": "text"}, {"items": 5}, {}])
def test_a_wildcard_over_anything_but_a_list_gives_an_empty_list(data):
    assert get_path(data, "items[*].id") == []


def test_an_empty_wildcard_falls_through_to_the_next_alternative():
    data = {"items": [], "backup": [{"name": "B"}]}
    assert get_path(data, "items[*].name|backup[*].name") == ["B"]
    assert get_path(data, "items[*].name|missing[*].name") == []


def test_an_empty_wildcard_gives_a_custom_default_when_one_is_passed():
    assert get_path({"items": []}, "items[*].id", ["none"]) == ["none"]


def test_fallbacks_can_mix_wildcard_and_plain_paths():
    assert get_path({"items": [{"url": "a"}]}, "download_url|items[*].url") == ["a"]


@pytest.mark.parametrize(
    "segment", ["1_0", "+1", "١"], ids=["underscore", "plus", "arabic-one"]
)
def test_only_plain_digits_index_a_list(segment):
    # Regression: int() took these as indexes too.
    assert get_path({"a": list(range(20))}, f"a.{segment}") is None
