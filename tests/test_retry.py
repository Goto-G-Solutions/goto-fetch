"""Retry: when a failed request is tried again, and the wait before each try."""

from datetime import UTC, datetime

import httpx2
import pytest
from support import replies

from api_explorer import ApiError, Retry, backoff_delay, retry_after_seconds

OK = httpx2.Response(200, json={"ok": True})


def test_a_server_error_is_retried_and_each_retry_is_logged(mock_client, caplog):
    handler, calls = replies(httpx2.Response(503))
    with pytest.raises(ApiError) as raised:
        mock_client(handler, retry=Retry(attempts=3, backoff=0)).request(
            "GET", "/items/"
        )
    assert raised.value.status == 503
    assert len(calls) == 3
    retries = [m.rsplit("; ", 1)[1].split(" in ")[0] for m in caplog.messages]
    assert retries == ["retry 1 of 2", "retry 2 of 2"]


@pytest.mark.parametrize(
    "first",
    [httpx2.Response(429), httpx2.Response(408), httpx2.ConnectError("refused")],
    ids=["rate-limited", "request-timeout", "network-error"],
)
def test_temporary_failures_are_retried(mock_client, first):
    handler, calls = replies(first, OK)
    assert mock_client(handler).request("GET", "/items/").status_code == 200
    assert len(calls) == 2


@pytest.mark.parametrize("status", [404, 501, 505])
def test_failures_that_wont_change_are_not_retried(mock_client, status):
    handler, calls = replies(httpx2.Response(status))
    with pytest.raises(ApiError):
        mock_client(handler).request("GET", "/items/")
    assert len(calls) == 1


def test_the_statuses_worth_retrying_can_be_chosen(mock_client):
    handler, calls = replies(httpx2.Response(404), OK)
    retry = Retry(backoff=0, statuses=frozenset({404}))
    assert (
        mock_client(handler, retry=retry).request("GET", "/items/").status_code == 200
    )
    assert len(calls) == 2


def test_post_is_retried_only_when_asked(mock_client):
    handler, calls = replies(httpx2.Response(503))
    with pytest.raises(ApiError):
        mock_client(handler).request("POST", "/items/", json={})
    assert len(calls) == 1
    with pytest.raises(ApiError):
        mock_client(handler).request("POST", "/items/", json={}, retry=True)
    assert len(calls) == 1 + 3


def test_the_methods_retried_by_default_can_be_chosen(mock_client):
    handler, calls = replies(httpx2.Response(503), OK)
    retry = Retry(backoff=0, methods=frozenset({"POST"}))
    assert (
        mock_client(handler, retry=retry).request("POST", "/items/").status_code == 200
    )
    assert len(calls) == 2


def test_a_retry_after_beyond_max_backoff_fails_at_once(mock_client):
    handler, calls = replies(httpx2.Response(429, headers={"Retry-After": "999"}))
    with pytest.raises(ApiError, match="asks to wait 999s"):
        mock_client(handler, retry=Retry(max_backoff=1.0)).request("GET", "/items/")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"attempts": 0}, "attempts must be at least 1"),
        ({"backoff": -1}, "can't be negative"),
    ],
)
def test_retry_refuses_impossible_settings(settings, message):
    with pytest.raises(ValueError, match=message):
        Retry(**settings)


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
