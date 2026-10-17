"""api_explorer.testing: the fake APIs' own promises.

offset_api, TokenServer, json_pages and replies are exercised throughout
test_paging, test_auth, test_client and test_retry, which check their
options as they test the client. This file covers the rest: FakeApi's
record of calls, flaky, and the builders the other tests don't use.
"""

import httpx2
import pytest

from api_explorer import ApiError, Cursor, HeaderLink, Retry
from api_explorer.testing import (
    FakeApi,
    cursor_api,
    flaky,
    link_header_api,
    next_link_api,
    replies,
)


def test_a_fake_api_remembers_each_request_in_forms_tests_check():
    api = replies(200)
    client = api.client()
    client.request("POST", "/items/?a=1", json={"name": "Ann"})
    client.request("GET", "/other/")
    assert api.paths == ["/api/items/", "/api/other/"]
    assert api.queries == [{"a": "1"}, {}]
    assert api.bodies == [{"name": "Ann"}, None]


def test_a_fake_api_s_client_retries_without_waiting_unless_told_otherwise():
    assert FakeApi(lambda request: httpx2.Response(200)).client().retry == Retry(
        backoff=0
    )
    custom = Retry(attempts=1)
    assert (
        FakeApi(lambda request: httpx2.Response(200)).client(retry=custom).retry
        is custom
    )


def test_replies_take_responses_status_codes_and_exceptions_in_order():
    api = replies(
        httpx2.ConnectError("refused"), 503, httpx2.Response(200, json={"ok": True})
    )
    assert api.client().get_json("/x/") == {"ok": True}
    assert len(api.calls) == 3


def test_replies_need_at_least_one_response():
    with pytest.raises(ValueError, match="at least one"):
        replies()


def test_flaky_fails_first_as_told_then_lets_the_api_answer():
    api = flaky(next_link_api(3), 503, httpx2.Response(429))
    assert list(api.client().records("/items/")) == [0, 1, 2]
    assert len(api.calls) == 3


def test_flaky_composes_and_its_failures_reach_the_client():
    api = flaky(flaky(replies(200), 503), 404)
    with pytest.raises(ApiError) as raised:
        api.client().request("GET", "/x/")
    assert raised.value.status == 404


def test_next_link_api_pages_like_django_rest_framework():
    api = next_link_api(250, page_size=100)
    assert list(api.client().records("/items/", count_key="count")) == list(range(250))
    assert api.queries == [{}, {"page": "2"}, {"page": "3"}]


def test_link_header_api_pages_by_its_link_header():
    api = link_header_api([{"id": i} for i in range(5)], page_size=2)
    records = api.client().records("/repos/", results_key=None, paging=HeaderLink())
    assert [r["id"] for r in records] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("send_in", ["query", "body"])
def test_cursor_api_takes_its_cursor_where_it_is_told(send_in):
    api = cursor_api(5, page_size=2, send_in=send_in)
    method = "POST" if send_in == "body" else "GET"
    records = api.client().records(
        "/events/",
        method=method,
        json={} if send_in == "body" else None,
        results_key="data",
        paging=Cursor(send_in=send_in),
    )
    assert list(records) == [0, 1, 2, 3, 4]
    sent = api.bodies if send_in == "body" else api.queries
    assert [s.get("cursor") for s in sent] == [None, "2", "4"]
