"""Paging: the paginate loop, Client.records, NextLink, Offset, and progress logs.

records() is exercised through a mock client because it is where paging,
results paths and counts meet; each test still checks one behaviour.
"""

import json
import re

import httpx2
import pytest

from api_explorer import (
    ApiError,
    Cursor,
    HeaderLink,
    NextLink,
    Offset,
    Page,
    PageRequest,
    paginate,
    read_page,
    request_key,
    with_query,
)
from api_explorer.testing import FakeApi, json_pages, offset_api, replies

BASE = "http://api.test/api/items/"


def test_paginate_fetches_lazily_and_resolves_relative_links():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return {"next": "?page=2" if url.endswith("/") else None}

    pages = paginate(fetch, "http://h/items/", lambda page, url: page["next"])
    assert next(pages) == {"next": "?page=2"}
    assert fetched == ["http://h/items/"]
    list(pages)
    assert fetched == ["http://h/items/", "http://h/items/?page=2"]


def test_paginate_stops_with_an_error_on_a_loop():
    pages = paginate(lambda url: url, "http://h/a", lambda page, url: "http://h/a")
    with pytest.raises(ApiError, match="Pagination loops"):
        list(pages)


def test_paginate_can_page_by_request_body_as_well_as_url():
    # A POST search API: the same URL each time, the cursor in the body.
    pages = {None: {"rows": [1], "cursor": "c2"}, "c2": {"rows": [2], "cursor": None}}

    def fetch(body):
        return pages[body.get("cursor")]

    def next_request(page, body):
        return {**body, "cursor": page["cursor"]} if page["cursor"] else None

    rows = [
        row
        for page in paginate(fetch, {"q": "x"}, next_request)
        for row in page["rows"]
    ]
    assert rows == [1, 2]


def test_paginate_detects_a_loop_in_request_bodies():
    with pytest.raises(ApiError, match="Pagination loops"):
        list(paginate(lambda body: body, {"q": "x"}, lambda page, body: {"q": "x"}))


def test_paginate_tells_pages_apart_by_the_key_you_give():
    # Bodies differ in a field that doesn't identify the page; keyed by
    # cursor, the repeat is caught.
    pages = paginate(
        lambda body: body,
        {"cursor": "a", "sent": 1},
        lambda page, body: {"cursor": "a", "sent": body["sent"] + 1},
        key=lambda body: body["cursor"],
    )
    with pytest.raises(ApiError, match="Pagination loops"):
        list(pages)


def test_request_key_is_the_url_or_the_json_form_with_sorted_keys():
    assert request_key("http://h/a") == "http://h/a"
    assert request_key({"b": 1, "a": 2}) == request_key({"a": 2, "b": 1})


def test_records_follow_a_link_header_with_header_link(mock_client):
    def handler(request):
        page = request.url.params.get("page", "1")
        headers = {"Link": '<?page=2>; rel="next"'} if page == "1" else {}
        return httpx2.Response(200, json=[int(page)], headers=headers)

    records = mock_client(handler).records(
        "/repos/", results_key=None, paging=HeaderLink()
    )
    assert list(records) == [1, 2]


def search_api(pages: dict) -> tuple:
    """A POST search endpoint: the body's "cursor" picks the page. Also logs bodies."""
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append((request.method, body))
        return httpx2.Response(200, json=pages[body.get("cursor")])

    return handler, bodies


def test_records_page_a_post_search_by_a_cursor_in_the_body(mock_client):
    handler, bodies = search_api(
        {
            None: {"hits": [1, 2], "next_cursor": "c2"},
            "c2": {"hits": [3], "next_cursor": None},
        }
    )
    records = mock_client(handler).records(
        "/search/",
        method="POST",
        json={"q": "late"},
        results_key="hits",
        paging=Cursor(send_in="body"),
    )
    assert list(records) == [1, 2, 3]
    assert bodies == [("POST", {"q": "late"}), ("POST", {"q": "late", "cursor": "c2"})]


def test_a_cursor_can_go_back_in_the_query(mock_client):
    def handler(request):
        cursor = request.url.params.get("after")
        page = {
            None: {"data": [1], "meta": {"next": "x"}},
            "x": {"data": [2], "meta": {}},
        }
        return httpx2.Response(200, json=page[cursor])

    paging = Cursor(key="meta.next", param="after")
    assert list(
        mock_client(handler).records("/events/", results_key="data", paging=paging)
    ) == [1, 2]


def test_a_next_url_keeps_the_post_method_and_body(mock_client):
    # The Paging rule: a URL from a style keeps the method and body, so
    # URL-based styles (NextLink here) work for POST searches too.
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), json.loads(request.content)))
        last = "page=2" in str(request.url)
        return httpx2.Response(
            200, json={"results": [1], "next": None if last else "?page=2"}
        )

    list(mock_client(handler).records("/search/", method="post", json={"q": "x"}))
    assert seen == [
        ("POST", f"{BASE.replace('items', 'search')}", {"q": "x"}),
        ("POST", f"{BASE.replace('items', 'search')}?page=2", {"q": "x"}),
    ]


def test_records_pass_retry_through_so_a_post_search_can_be_retried(mock_client):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx2.Response(503)
        return httpx2.Response(200, json={"results": [1], "next": None})

    records = mock_client(handler).records(
        "/search/", method="POST", json={}, retry=True
    )
    assert list(records) == [1]
    assert len(calls) == 2


def test_a_cursor_that_repeats_is_caught_as_a_loop(mock_client):
    handler, _ = search_api(
        {
            None: {"hits": [1], "next_cursor": "c"},
            "c": {"hits": [2], "next_cursor": "c"},
        }
    )
    records = mock_client(handler).records(
        "/search/",
        method="POST",
        json={},
        results_key="hits",
        paging=Cursor(send_in="body"),
    )
    with pytest.raises(ApiError, match="Pagination loops"):
        list(records)


def test_cursor_refuses_an_unknown_place_to_send_it():
    with pytest.raises(ValueError, match="send_in must be"):
        Cursor(send_in="header")


def test_request_key_compares_page_requests_by_value():
    first = PageRequest("http://h/s", "POST", {"q": "x", "cursor": "c"})
    same = PageRequest("http://h/s", "POST", {"cursor": "c", "q": "x"})
    assert request_key(first) == request_key(same)
    assert request_key(first) != request_key(
        PageRequest("http://h/s", "POST", {"q": "y"})
    )


def test_records_follow_next_links_by_default(mock_client):
    handler = json_pages(
        {
            BASE: {"results": [1, 2], "next": f"{BASE}?page=2"},
            f"{BASE}?page=2": {"results": [3], "next": None},
        }
    )
    assert list(mock_client(handler).records("/items/")) == [1, 2, 3]


def test_records_add_params_to_the_first_url_only(mock_client):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        last = "page=2" in str(request.url)
        return httpx2.Response(
            200, json={"results": [1], "next": None if last else "?page=2&size=5"}
        )

    list(mock_client(handler).records("/items/", params={"size": 5}))
    # The next link carries its own query; params aren't added again.
    assert seen == [f"{BASE}?size=5", f"{BASE}?page=2&size=5"]


def test_records_stop_at_max_pages(mock_client):
    handler = json_pages(
        {
            BASE: {"results": [1], "next": "?page=2"},
            f"{BASE}?page=2": {"results": [2], "next": None},
        }
    )
    assert list(mock_client(handler).records("/items/", max_pages=1)) == [1]


@pytest.mark.parametrize(
    ("page", "results_key", "count_key", "expected"),
    [
        ({"results": [1, 2], "count": 9}, "results", "count", Page([1, 2], 9)),
        ({"data": {"rows": [1]}}, "results|data.rows", None, Page([1], None)),
        ([1, 2], None, None, Page([1, 2], None)),
        ({"results": None}, "results", None, Page([], None)),
        ({"results": [], "count": "9"}, "results", "count", Page([], 9)),
        ({"results": [], "count": True}, "results", "count", Page([], None)),
    ],
    ids=[
        "results-and-count",
        "fallback-path",
        "bare-list",
        "null-results",
        "count-as-text",
        "count-as-bool",
    ],
)
def test_read_page_finds_the_records_and_a_whole_number_count(
    page, results_key, count_key, expected
):
    assert read_page(page, results_key, count_key) == expected


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ({"data": [1]}, "No 'results' in the response"),
        ({"results": {"a": 1}}, "'results' holds a dict, not a list"),
    ],
    ids=["missing-results", "results-not-a-list"],
)
def test_read_page_refuses_a_page_of_the_wrong_shape(page, message):
    with pytest.raises(ApiError, match=re.escape(message)) as raised:
        read_page(page, url="http://h/items/")
    assert raised.value.url == "http://h/items/"


def test_records_read_pages_with_read_page_and_name_the_url(mock_client):
    # Overlaps the read_page tests on purpose: this checks records() uses it
    # and passes its URL, so a bad page names where it came from.
    with pytest.raises(ApiError, match="No 'results'") as raised:
        list(mock_client(json_pages({BASE: {"data": []}})).records("/items/"))
    assert raised.value.url == BASE


def test_records_follow_a_next_link_at_any_path(mock_client):
    handler = json_pages(
        {"http://api.test/api/geo/": {"results": [1], "meta": {"next": None}}}
    )
    assert list(
        mock_client(handler).records("/geo/", paging=NextLink("meta.next"))
    ) == [1]


def test_records_refuse_a_next_link_that_is_not_a_string(mock_client):
    with pytest.raises(ApiError, match="'next' holds a int, not a URL"):
        list(
            mock_client(json_pages({BASE: {"results": [], "next": 2}})).records(
                "/items/"
            )
        )


def test_records_log_a_page_event_per_page_and_a_done_event(mock_client, log_info):
    handler = json_pages(
        {
            BASE: {"count": 3, "results": [1, 2], "next": "?page=2"},
            f"{BASE}?page=2": {"count": 3, "results": [3], "next": None},
        }
    )
    list(mock_client(handler).records("/items/", count_key="count"))
    pages = [r for r in log_info.records if getattr(r, "event", None) == "page"]
    assert re.fullmatch(
        r"Page 1: 2 records in \d+\.\d\ds, 2 so far of 3", pages[0].getMessage()
    )
    assert re.fullmatch(
        r"Page 2: 1 records in \d+\.\d\ds, 3 so far of 3", pages[1].getMessage()
    )
    # The same numbers come as data, for progress bars and the like.
    assert [
        {k: r.data[k] for k in ("page", "records_on_page", "records_so_far", "total")}
        for r in pages
    ] == [
        {"page": 1, "records_on_page": 2, "records_so_far": 2, "total": 3},
        {"page": 2, "records_on_page": 1, "records_so_far": 3, "total": 3},
    ]
    done = [r for r in log_info.records if getattr(r, "event", None) == "done"]
    assert [(r.data["pages"], r.data["records"]) for r in done] == [(2, 3)]


def test_progress_leaves_out_the_total_without_a_count(mock_client, log_info):
    handler = json_pages({BASE: {"results": [1], "next": None}})
    list(mock_client(handler).records("/items/"))
    pages = [r for r in log_info.records if getattr(r, "event", None) == "page"]
    assert pages[0].getMessage().endswith("1 so far")
    assert pages[0].data["total"] is None


def test_records_warn_once_when_the_count_is_missing(mock_client, caplog):
    handler = json_pages(
        {
            BASE: {"results": [1], "next": "?page=2"},
            f"{BASE}?page=2": {"results": [2], "next": None},
        }
    )
    list(mock_client(handler).records("/items/", count_key="count"))
    assert caplog.messages == ["No count at 'count' in the response"]


def fetch_offset(mock_client, handler, count_key=None, **paging):
    """Fetch every record from an offset_api handler."""
    client = mock_client(handler)
    return list(
        client.records(
            "/items/", results_key="items", count_key=count_key, paging=Offset(**paging)
        )
    )


def test_offset_stops_at_the_count_without_an_extra_request(mock_client):
    handler = offset_api(250)
    assert fetch_offset(mock_client, handler, "count", limit=100) == list(range(250))
    assert [c["offset"] for c in handler.queries] == ["0", "100", "200"]


def test_offset_survives_a_server_that_caps_the_page_size(mock_client):
    # Pages of 100 when 1,000 were asked for must not look like the last page.
    handler = offset_api(250, cap=100, count=False)
    assert fetch_offset(mock_client, handler, limit=1000) == list(range(250))
    assert [c["offset"] for c in handler.queries] == ["0", "100", "200"]


@pytest.mark.parametrize(
    ("total", "requests"), [(200, 3), (37, 2)], ids=["exact-multiple", "one-page"]
)
def test_offset_without_a_count_ends_on_an_empty_page(mock_client, total, requests):
    handler = offset_api(total, count=False)
    assert fetch_offset(mock_client, handler, limit=100) == list(range(total))
    assert len(handler.queries) == requests


def test_offset_warns_when_records_run_out_before_the_count(mock_client, caplog):
    handler = offset_api(230, count=250)
    assert fetch_offset(mock_client, handler, "count", limit=100) == list(range(230))
    assert caplog.messages == ["Records ran out at 230 of the 250 the count promised"]


def test_offset_refuses_a_server_that_ignores_the_offset(mock_client):
    handler = offset_api(250, count=False, ignore_offset=True)
    with pytest.raises(ApiError, match="seems to ignore 'offset'"):
        fetch_offset(mock_client, handler, limit=100)
    assert len(handler.queries) == 2


def test_offset_starts_from_the_offset_in_the_endpoint_and_keeps_the_query(mock_client):
    handler = offset_api(250)
    records = mock_client(handler).records(
        "/items/?q=x&offset=200",
        results_key="items",
        count_key="count",
        paging=Offset(limit=100),
    )
    assert list(records) == list(range(200, 250))
    assert handler.queries == [{"q": "x", "offset": "200", "limit": "100"}]


def test_offset_names_its_own_query_parameters(mock_client):
    seen = []

    def handler(request):
        query = dict(request.url.params)
        seen.append(query)
        start = int(query["skip"])
        return httpx2.Response(200, json={"items": list(range(5))[start : start + 2]})

    paging = Offset(limit=2, limit_param="take", offset_param="skip")
    assert list(
        mock_client(handler).records("/items/", results_key="items", paging=paging)
    ) == [0, 1, 2, 3, 4]
    assert [q["skip"] for q in seen] == ["0", "2", "4"]


def test_offset_refuses_a_limit_below_one():
    with pytest.raises(ValueError, match="limit must be at least 1"):
        Offset(limit=0)


def test_with_query_sets_one_parameter_and_keeps_the_rest():
    assert with_query("http://h/a?q=x&page=1", "page", 2) == "http://h/a?q=x&page=2"
    assert with_query("http://h/a", "after", "c 1") == "http://h/a?after=c+1"


def endless_api(style_key: str, rows_until: int):
    """A server that keeps sending a next link or cursor after the data ends."""

    def answer(request):
        n = int(request.url.params.get("n", 0))
        rows = [n] if n < rows_until else []
        following = f"?n={n + 1}" if style_key == "next" else str(n + 1)
        return httpx2.Response(200, json={"results": rows, style_key: following})

    return answer


@pytest.mark.parametrize(
    ("paging", "key"),
    [(NextLink(), "next"), (Cursor(key="next_cursor", param="n"), "next_cursor")],
    ids=["next-link", "cursor"],
)
def test_an_empty_page_ends_the_run_even_if_more_is_promised(mock_client, paging, key):
    # Regression (dlt #3358): an API that keeps sending a cursor or next link
    # after the data ends made the run page forever.
    api = FakeApi(endless_api(key, rows_until=2))
    assert list(mock_client(api).records("/e/", paging=paging, max_pages=50)) == [0, 1]
    assert len(api.calls) == 3


def test_stop_on_empty_false_keeps_paging_past_an_empty_page(mock_client):
    pages = {
        BASE: {"results": [1], "next": "?p=2"},
        f"{BASE}?p=2": {"results": [], "next": "?p=3"},
        f"{BASE}?p=3": {"results": [3], "next": None},
    }
    records = mock_client(json_pages(pages)).records(
        "/items/", paging=NextLink(stop_on_empty=False)
    )
    assert list(records) == [1, 3]


def test_a_next_path_missing_from_the_first_page_is_warned_about(mock_client, caplog):
    # Regression (dlt #2505): an unquoted "@odata.nextLink" is read as a
    # nested path, so paging stopped after one page without a word.
    pages = {
        BASE: {"value": [1], "@odata.nextLink": f"{BASE}?p=2"},
        f"{BASE}?p=2": {"value": [2]},
    }
    client = mock_client(json_pages(pages))
    assert list(
        client.records(
            "/items/", results_key="value", paging=NextLink("@odata.nextLink")
        )
    ) == [1]
    assert any("No '@odata.nextLink' in the first page" in m for m in caplog.messages)
    quoted = NextLink('["@odata.nextLink"]')
    assert list(client.records("/items/", results_key="value", paging=quoted)) == [1, 2]


def test_a_cursor_path_that_finds_several_values_is_refused(mock_client):
    # Regression (dlt #3397): the cursor became the text of a list.
    page = {BASE: {"results": [{"c": "a"}, {"c": "b"}]}}
    with pytest.raises(
        ApiError, match=re.escape("'results[*].c' holds a list, not one cursor value")
    ):
        list(
            mock_client(json_pages(page)).records(
                "/items/", paging=Cursor(key="results[*].c")
            )
        )


def test_a_malformed_next_link_is_refused_even_on_an_empty_page(mock_client):
    with pytest.raises(ApiError, match="holds a int, not a URL"):
        list(
            mock_client(json_pages({BASE: {"results": [], "next": 2}})).records(
                "/items/"
            )
        )


def test_an_empty_body_or_204_is_an_empty_page(mock_client):
    # Regression (dlt #1849): an empty 204 failed as "did not return JSON".
    assert list(mock_client(replies(httpx2.Response(204))).records("/items/")) == []
    assert (
        list(mock_client(replies(httpx2.Response(200, content=b""))).records("/items/"))
        == []
    )
