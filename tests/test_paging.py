"""Paging: the paginate loop, Client.records, NextLink, Offset, and progress logs.

records() is exercised through a mock client because it is where paging,
results paths and counts meet; each test still checks one behaviour.
"""

import re

import httpx2
import pytest
from support import offset_api, pages_at

from api_explorer import ApiError, NextLink, Offset, paginate

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


def test_records_follow_next_links_by_default(mock_client):
    handler = pages_at(
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
            200, json={"results": [], "next": None if last else "?page=2&size=5"}
        )

    list(mock_client(handler).records("/items/", params={"size": 5}))
    # The next link carries its own query; params aren't added again.
    assert seen == [f"{BASE}?size=5", f"{BASE}?page=2&size=5"]


def test_records_stop_at_max_pages(mock_client):
    handler = pages_at(
        {
            BASE: {"results": [1], "next": "?page=2"},
            f"{BASE}?page=2": {"results": [2], "next": None},
        }
    )
    assert list(mock_client(handler).records("/items/", max_pages=1)) == [1]


def test_records_read_results_and_next_by_path_or_take_a_bare_list(mock_client):
    handler = pages_at(
        {
            "http://api.test/api/geo/": {
                "features": [{"id": 1}],
                "meta": {"next": None},
            },
            "http://api.test/api/plain/": [{"id": 2}],
        }
    )
    client = mock_client(handler)
    geo = client.records(
        "/geo/", results_key="results|features", paging=NextLink("meta.next")
    )
    assert list(geo) == [{"id": 1}]
    assert list(client.records("/plain/", results_key=None)) == [{"id": 2}]


def test_records_treat_null_results_as_an_empty_page(mock_client):
    handler = pages_at({BASE: {"results": None, "next": None}})
    assert list(mock_client(handler).records("/items/")) == []


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ({"data": [1], "next": None}, "No 'results' in the response"),
        ({"results": {"a": 1}, "next": None}, "'results' holds a dict, not a list"),
        ({"results": [], "next": 2}, "'next' holds a int, not a URL"),
    ],
    ids=["missing-results", "results-not-a-list", "next-not-a-string"],
)
def test_records_refuse_a_page_of_the_wrong_shape(mock_client, page, message):
    with pytest.raises(ApiError, match=re.escape(message)):
        list(mock_client(pages_at({BASE: page})).records("/items/"))


def test_records_log_one_progress_line_per_page(mock_client, log_info):
    handler = pages_at(
        {
            BASE: {"count": 3, "results": [1, 2], "next": "?page=2"},
            f"{BASE}?page=2": {"count": 3, "results": [3], "next": None},
        }
    )
    list(mock_client(handler).records("/items/", count_key="count"))
    progress = [m for m in log_info.messages if m.startswith("Page ")]
    assert len(progress) == 2
    assert re.fullmatch(r"Page 1: 2 records in \d+\.\d\ds, 2 so far of 3", progress[0])
    assert re.fullmatch(r"Page 2: 1 records in \d+\.\d\ds, 3 so far of 3", progress[1])


def test_progress_leaves_out_the_total_without_a_count(mock_client, log_info):
    list(
        mock_client(pages_at({BASE: {"results": [1], "next": None}})).records("/items/")
    )
    assert log_info.messages[-1].endswith("1 so far")


def test_records_warn_once_when_the_count_is_missing(mock_client, caplog):
    handler = pages_at(
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
    handler, calls = offset_api(250)
    assert fetch_offset(mock_client, handler, "count", limit=100) == list(range(250))
    assert [c["offset"] for c in calls] == ["0", "100", "200"]


def test_offset_survives_a_server_that_caps_the_page_size(mock_client):
    # Pages of 100 when 1,000 were asked for must not look like the last page.
    handler, calls = offset_api(250, cap=100, count=None)
    assert fetch_offset(mock_client, handler, limit=1000) == list(range(250))
    assert [c["offset"] for c in calls] == ["0", "100", "200"]


@pytest.mark.parametrize(
    ("total", "requests"), [(200, 3), (37, 2)], ids=["exact-multiple", "one-page"]
)
def test_offset_without_a_count_ends_on_an_empty_page(mock_client, total, requests):
    handler, calls = offset_api(total, count=None)
    assert fetch_offset(mock_client, handler, limit=100) == list(range(total))
    assert len(calls) == requests


def test_offset_warns_when_records_run_out_before_the_count(mock_client, caplog):
    handler, _ = offset_api(230, count=250)
    assert fetch_offset(mock_client, handler, "count", limit=100) == list(range(230))
    assert caplog.messages == ["Records ran out at 230 of the 250 the count promised"]


def test_offset_refuses_a_server_that_ignores_the_offset(mock_client):
    handler, calls = offset_api(250, count=None, ignore_offset=True)
    with pytest.raises(ApiError, match="seems to ignore 'offset'"):
        fetch_offset(mock_client, handler, limit=100)
    assert len(calls) == 2


def test_offset_starts_from_the_offset_in_the_endpoint_and_keeps_the_query(mock_client):
    handler, calls = offset_api(250)
    records = mock_client(handler).records(
        "/items/?q=x&offset=200",
        results_key="items",
        count_key="count",
        paging=Offset(limit=100),
    )
    assert list(records) == list(range(200, 250))
    assert calls == [{"q": "x", "offset": "200", "limit": "100"}]


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
