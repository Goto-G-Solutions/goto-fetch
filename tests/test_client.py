"""Client: URLs, retries and errors, plus the retry helpers. Paging is in test_paging."""

from datetime import UTC, datetime

import httpx2
import pytest
from support import replies

from api_explorer import ApiError, Client, backoff_delay, retry_after_seconds

OK = httpx2.Response(200, json={"ok": True})


@pytest.mark.parametrize(
    ("endpoint", "url"),
    [
        ("/items", "http://api.test/api/items/"),
        ("items?page=2", "http://api.test/api/items/?page=2"),
        ("http://other.test/x?a=1", "http://other.test/x?a=1"),
    ],
)
def test_endpoints_join_the_base_url_with_a_trailing_slash(mock_client, endpoint, url):
    assert mock_client(replies(OK)[0]).url(endpoint) == url


def test_the_trailing_slash_can_be_turned_off(mock_client):
    client = mock_client(replies(OK)[0], trailing_slash=False)
    assert client.url("/items") == "http://api.test/api/items"


def test_request_returns_the_response_on_success(mock_client):
    handler, calls = replies(OK)
    assert mock_client(handler).request("GET", "/items/").json() == {"ok": True}
    assert str(calls[0].url) == "http://api.test/api/items/"


def test_a_client_error_fails_at_once_with_its_status_and_body(mock_client):
    handler, calls = replies(httpx2.Response(404, text="Not found."))
    with pytest.raises(ApiError, match="HTTP 404: Not found.") as raised:
        mock_client(handler).request("GET", "/items/1/")
    assert raised.value.status == 404
    assert len(calls) == 1


def test_a_server_error_is_retried_and_each_retry_is_logged(mock_client, caplog):
    handler, calls = replies(httpx2.Response(503))
    with pytest.raises(ApiError) as raised:
        mock_client(handler, attempts=3).request("GET", "/items/")
    assert raised.value.status == 503
    assert len(calls) == 3
    retries = [m.rsplit("; ", 1)[1].split(" in ")[0] for m in caplog.messages]
    assert retries == ["retry 1 of 2", "retry 2 of 2"]


@pytest.mark.parametrize(
    "first",
    [httpx2.Response(429), httpx2.ConnectError("refused")],
    ids=["rate-limited", "network-error"],
)
def test_a_rate_limit_or_network_error_is_retried(mock_client, first):
    handler, calls = replies(first, OK)
    assert mock_client(handler).request("GET", "/items/").status_code == 200
    assert len(calls) == 2


def test_post_is_not_retried_unless_asked(mock_client):
    handler, calls = replies(httpx2.Response(503))
    with pytest.raises(ApiError):
        mock_client(handler).request("POST", "/items/", json={})
    assert len(calls) == 1
    with pytest.raises(ApiError):
        mock_client(handler).request("POST", "/items/", json={}, retry=True)
    assert len(calls) == 1 + 3


def test_a_retry_after_beyond_max_backoff_fails_at_once(mock_client):
    handler, calls = replies(httpx2.Response(429, headers={"Retry-After": "999"}))
    with pytest.raises(ApiError, match="asks to wait 999s"):
        mock_client(handler, max_backoff=1.0).request("GET", "/items/")
    assert len(calls) == 1


def test_attempts_below_one_are_refused():
    with pytest.raises(ValueError, match="attempts must be at least 1"):
        Client("http://api.test", attempts=0)


def test_get_json_refuses_a_body_that_is_not_json(mock_client):
    handler, _ = replies(httpx2.Response(200, text="<html>"))
    with pytest.raises(ApiError, match="did not return JSON"):
        mock_client(handler).get_json("/items/")


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("5", 5.0),
        ("Thu, 01 Jan 2026 12:00:30 GMT", 30.0),
        ("Thu, 01 Jan 2026 11:00:00 GMT", 0.0),
        ("soon", None),
        (None, None),
    ],
)
def test_retry_after_reads_seconds_or_an_http_date(header, seconds):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert retry_after_seconds(header, now) == seconds


@pytest.mark.parametrize("attempt", range(5))
def test_backoff_delay_doubles_within_its_jitter(attempt):
    expected = min(2**attempt, 30.0)
    assert expected * 0.8 <= backoff_delay(attempt) <= expected * 1.2


def test_backoff_delay_never_exceeds_its_cap_plus_jitter():
    assert all(backoff_delay(10, base=1.0, cap=5.0) <= 6.0 for _ in range(20))
