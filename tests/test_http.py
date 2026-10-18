"""goto_fetch.http: the package's own requests, responses, headers and URLs.

Everything above the transports speaks these types, so their rules are
checked here once: how parameters are encoded and URLs joined, how
headers ignore case, how bodies are built, and how a response is read.
The transports' own promises are in test_transports.
"""

import pytest

from goto_fetch.http import (
    URL,
    Headers,
    Request,
    Response,
    Timeout,
    is_absolute,
    join_url,
    set_param,
)


@pytest.mark.parametrize(
    ("url", "name", "value", "result"),
    [
        ("http://h/x?a=1&b=2", "a", 9, "http://h/x?a=9&b=2"),
        ("http://h/x?a=1&b=2&a=3", "a", 9, "http://h/x?a=9&b=2"),
        ("http://h/x", "flag", True, "http://h/x?flag=true"),
        ("http://h/x", "q", "late orders", "http://h/x?q=late+orders"),
        ("http://h/x?q=late%20orders", "page", 2, "http://h/x?q=late%20orders&page=2"),
        ("http://h/x#top", "page", 2, "http://h/x?page=2#top"),
        ("http://h/x", "empty", None, "http://h/x?empty="),
        ("http://h/x", "ids", [1, 2], "http://h/x?ids=1&ids=2"),
        ("http://h/x?filter%5Bx%5D=1", "filter[x]", 2, "http://h/x?filter%5Bx%5D=2"),
    ],
    ids=[
        "in-place",
        "repeats-become-one",
        "true",
        "space",
        "keeps-encoding",
        "keeps-fragment",
        "none-is-empty",
        "list-repeats",
        "encoded-name",
    ],
)
def test_set_param_sets_one_parameter_and_keeps_the_rest_as_written(
    url, name, value, result
):
    assert set_param(url, name, value) == result


@pytest.mark.parametrize(
    ("base", "endpoint", "joined"),
    [
        ("http://h/api", "items/", "http://h/api/items/"),
        ("http://h/api/", "/items/?a=1", "http://h/api/items/?a=1"),
        ("http://h/api?key=1", "items/", "http://h/api/items/"),
        ("http://h/api", "projects:list", "http://h/api/projects:list"),
        ("http://h/api", "https://other/x", "https://other/x"),
        ("http://h/api", "//other/x", "//other/x"),
    ],
    ids=[
        "plain",
        "leading-slash",
        "base-query",
        "colon-in-path",
        "absolute",
        "protocol-relative",
    ],
)
def test_join_url_puts_an_endpoint_under_the_base(base, endpoint, joined):
    assert join_url(base, endpoint) == joined


def test_a_path_with_a_colon_isn_t_taken_for_a_url():
    assert not is_absolute("projects:list")
    assert is_absolute("https://h/") and is_absolute("//h/")


def test_headers_ignore_case_in_every_way():
    headers = Headers([("Accept", "a"), ("X-Tag", "1"), ("x-tag", "2")])
    assert headers["ACCEPT"] == "a" and "accept" in headers
    assert headers.get_list("X-TAG") == ["1", "2"]
    assert list(headers) == ["Accept", "X-Tag"]  # one name each, as first written
    assert len(headers) == 2
    headers["x-TAG"] = "3"
    assert headers.get_list("x-tag") == ["3"]
    del headers["accept"]
    assert "Accept" not in headers
    with pytest.raises(KeyError):
        del headers["missing"]


def test_a_request_builds_each_kind_of_body():
    assert Request("POST", "http://h", json={"a": 1}).content == b'{"a": 1}'
    form = Request("POST", "http://h", data={"n": None, "ids": [1, 2]})
    assert (form.content, form.headers["content-type"]) == (
        b"n=&ids=1&ids=2",
        "application/x-www-form-urlencoded",
    )
    typed = Request(
        "POST", "http://h", json={}, headers={"content-type": "application/vnd+json"}
    )
    assert typed.headers.get_list("Content-Type") == ["application/vnd+json"]
    assert Request("GET", "http://h/x", params={"a": 1}).url == "http://h/x?a=1"


def test_a_request_with_json_none_has_no_body():
    # Regression: json=None built the body b"null" with a JSON Content-Type,
    # so every GET that records() sent carried one.
    plain = Request("GET", "http://h", json=None)
    assert plain.content == b"" and "Content-Type" not in plain.headers
    assert Response(200, json=None).content == b""


def test_headers_built_from_headers_keep_repeats():
    # Regression: a Headers is a Mapping, so copying one joined repeated
    # names into one value, "a, b".
    repeated = Headers([("Link", "a"), ("Link", "b")])
    assert Headers(repeated).raw() == [("Link", "a"), ("Link", "b")]


def test_a_request_takes_one_body():
    with pytest.raises(ValueError, match="one of content, json or data"):
        Request("POST", "http://h", json={}, data={"a": 1})


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain; charset=iso-8859-1",
        'text/plain; charset="iso-8859-1"',
        "text/plain; Charset=ISO-8859-1",
    ],
    ids=["bare", "quoted", "capitals"],
)
def test_response_text_reads_the_charset_however_it_is_written(content_type):
    # Regression: a quoted or capitalised charset was missed, giving "�".
    response = Response(
        200, content="é".encode("latin-1"), headers={"Content-Type": content_type}
    )
    assert response.text == "é"


def test_response_links_follow_rfc_8288():
    response = Response(
        200,
        headers=[
            ("Link", '<http://h/2?a=1,2>; REL="next last"; title="a;b, c"'),
            ("Link", "<http://h/1>; rel=prev"),
        ],
    )
    links = response.links
    assert links["next"]["url"] == links["last"]["url"] == "http://h/2?a=1,2"
    assert links["next"]["title"] == "a;b, c"
    assert links["prev"]["url"] == "http://h/1"


def test_a_timeout_of_one_number_applies_to_both_stages():
    assert Timeout.of(20) == Timeout(20, 20)
    assert Timeout.of(None) == Timeout(None, None)
    limits = Timeout(connect=5, read=60)
    assert Timeout.of(limits) is limits


def test_a_url_gives_every_value_of_a_repeated_parameter():
    url = URL("http://h/x?status=open&page=2&status=pending")
    assert url.get_list("status") == ["open", "pending"]
    assert url.params["status"] == "pending"  # the mapping keeps the last
    assert url.get_list("missing") == []
