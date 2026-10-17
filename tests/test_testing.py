"""api_explorer.testing: the fake APIs' own promises.

offset_api, TokenServer, json_pages and replies are exercised throughout
test_paging, test_auth, test_client and test_retry, which check their
options as they test the client. This file covers the rest: FakeApi's
record of calls, flaky, the builders the other tests don't use, and the
helpers for testing pieces of your own (fetched and the two checks), on
both sides: correct pieces pass, and each kind of broken piece is caught.
"""

import re

import httpx2
import pytest

from api_explorer import (
    ApiError,
    Cursor,
    HeaderLink,
    NextLink,
    Offset,
    PageRequest,
    Retry,
    read_page,
)
from api_explorer.testing import (
    FakeApi,
    check_paging,
    check_retry_policy,
    cursor_api,
    fetched,
    flaky,
    link_header_api,
    next_link_api,
    offset_api,
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


def test_fetched_builds_the_page_a_paging_style_receives():
    page = fetched(
        {"results": [1], "next": "?page=2"},
        headers={"Link": '<http://h/x?page=2>; rel="next"'},
    )
    first = PageRequest("http://api.test/api/items/")
    _, body_link = NextLink().begin(first, read_page_of)
    _, header_link = HeaderLink().begin(first, read_page_of)
    assert body_link(page, first) == "?page=2"
    assert header_link(page, first) == "http://h/x?page=2"
    assert (page.response.status_code, str(page.response.url)) == (200, first.url)


def read_page_of(page):
    return read_page(page.body)


class Bounded:
    """A custom policy that keeps the rules: a few short waits, then stop."""

    def wait(self, failure):
        return 0.1 if failure.attempt < 3 else None


class Refusing:
    """A custom policy that stops by raising ApiError, which also counts."""

    def wait(self, failure):
        raise ApiError("no retries here")


@pytest.mark.parametrize(
    "policy", [Retry(), Bounded(), Refusing()], ids=["Retry", "bounded", "raises"]
)
def test_check_retry_policy_passes_policies_that_keep_the_rules(policy):
    check_retry_policy(policy)


class Waits:
    """A broken policy that always asks for the same wait."""

    def __init__(self, seconds):
        self.seconds = seconds

    def wait(self, failure):
        return self.seconds


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (
            Waits(0.0),
            "after a 503 that never goes away, the policy was still retrying after 100 tries",
        ),
        (Waits(-1.0), "after a 503, the policy asked to wait -1.0"),
        (Waits(float("inf")), "asked to wait inf"),
        (Waits(float("nan")), "asked to wait nan"),
        (Waits(True), "asked to wait True"),
    ],
    ids=["never-stops", "negative", "endless", "nan", "not-seconds"],
)
def test_check_retry_policy_catches_a_broken_policy(policy, message):
    with pytest.raises(AssertionError, match=re.escape(message)):
        check_retry_policy(policy)


@pytest.mark.parametrize(
    ("paging", "api", "options"),
    [
        (NextLink(), next_link_api(250), {}),
        (Offset(limit=40), offset_api(250, cap=25), {"results_key": "items"}),
        (
            Cursor(send_in="body"),
            cursor_api(250, send_in="body"),
            {"results_key": "data", "method": "POST", "json": {}},
        ),
    ],
    ids=["next-link", "offset-with-cap", "post-cursor"],
)
def test_check_paging_passes_styles_that_get_every_record_once(paging, api, options):
    check_paging(paging, api, list(range(250)), **options)


class StopsEarly:
    """A broken style that never asks for a second page."""

    def begin(self, first, read):
        return first, lambda fetched, request: None


class SamePage:
    """A broken style that asks for the page it just fetched."""

    def begin(self, first, read):
        return first, lambda fetched, request: request.url


class NeverStops:
    """A broken style that keeps asking for the next page number, past the end."""

    def begin(self, first, read):
        def next_link(fetched, request):
            page = int(httpx2.URL(request.url).params.get("page", 1))
            return f"?page={page + 1}"

        return first, next_link


@pytest.mark.parametrize(
    ("paging", "message"),
    [
        (
            StopsEarly(),
            "didn't fetch the expected records: missing [100, 101, 102, 103, 104]",
        ),
        (SamePage(), "the paging style looped"),
        (NeverStops(), "didn't stop within 252 requests"),
    ],
    ids=["stops-early", "loops", "never-stops"],
)
def test_check_paging_catches_a_broken_style(paging, message):
    with pytest.raises(AssertionError, match=re.escape(message)):
        check_paging(paging, next_link_api(250), list(range(250)))
