"""Retry: when a failed request is tried again, and the wait before each try."""

from datetime import UTC, datetime

import httpx2
import pytest

from api_explorer import ApiError, Failure, Retry, backoff_delay, retry_after_seconds
from api_explorer.testing import replies

OK = httpx2.Response(200, json={"ok": True})


def test_a_server_error_is_retried_and_each_retry_is_logged(mock_client, caplog):
    handler = replies(httpx2.Response(503))
    with pytest.raises(ApiError) as raised:
        mock_client(handler, retry=Retry(attempts=3, backoff=0)).request(
            "GET", "/items/"
        )
    assert raised.value.status == 503
    assert len(handler.calls) == 3
    retries = [m.rsplit("; ", 1)[1].split(" in ")[0] for m in caplog.messages]
    assert retries == ["retry 1", "retry 2"]


@pytest.mark.parametrize(
    "first",
    [
        httpx2.Response(429),
        httpx2.Response(408),
        httpx2.ConnectError("refused"),
        # Regression (dlt b7500775): a connection dropped mid-response wasn't retried.
        httpx2.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        ),
    ],
    ids=["rate-limited", "request-timeout", "network-error", "dropped-mid-response"],
)
def test_temporary_failures_are_retried(mock_client, first):
    handler = replies(first, OK)
    assert mock_client(handler).request("GET", "/items/").status_code == 200
    assert len(handler.calls) == 2


@pytest.mark.parametrize("status", [404, 501, 505])
def test_failures_that_wont_change_are_not_retried(mock_client, status):
    handler = replies(httpx2.Response(status))
    with pytest.raises(ApiError):
        mock_client(handler).request("GET", "/items/")
    assert len(handler.calls) == 1


def test_the_statuses_worth_retrying_can_be_chosen(mock_client):
    handler = replies(httpx2.Response(404), OK)
    retry = Retry(backoff=0, statuses=frozenset({404}))
    assert (
        mock_client(handler, retry=retry).request("GET", "/items/").status_code == 200
    )
    assert len(handler.calls) == 2


def test_post_is_retried_only_when_asked(mock_client):
    handler = replies(httpx2.Response(503))
    with pytest.raises(ApiError):
        mock_client(handler).request("POST", "/items/", json={})
    assert len(handler.calls) == 1
    with pytest.raises(ApiError):
        mock_client(handler).request("POST", "/items/", json={}, retry=True)
    assert len(handler.calls) == 1 + 3


def test_the_methods_retried_by_default_can_be_chosen(mock_client):
    handler = replies(httpx2.Response(503), OK)
    retry = Retry(backoff=0, methods=frozenset({"POST"}))
    assert (
        mock_client(handler, retry=retry).request("POST", "/items/").status_code == 200
    )
    assert len(handler.calls) == 2


def test_a_retry_after_beyond_max_backoff_fails_at_once(mock_client):
    handler = replies(httpx2.Response(429, headers={"Retry-After": "999"}))
    with pytest.raises(ApiError, match="asks to wait 999s"):
        mock_client(handler, retry=Retry(max_backoff=1.0)).request("GET", "/items/")
    assert len(handler.calls) == 1


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


class Recording:
    """A retry policy that records each Failure, and waits as told."""

    def __init__(self, *waits):
        self.waits = list(waits)
        self.failures = []

    def wait(self, failure):
        self.failures.append(failure)
        return self.waits.pop(0) if self.waits else None


def test_a_policy_sees_each_failure_and_decides(mock_client):
    handler = replies(httpx2.Response(503), httpx2.ConnectError("refused"), OK)
    policy = Recording(0.0, 0.0)
    assert (
        mock_client(handler, retry=policy).request("GET", "/items/").status_code == 200
    )
    first, second = policy.failures
    assert (first.attempt, first.method, first.url) == (
        1,
        "GET",
        "http://api.test/api/items/",
    )
    assert first.response.status_code == 503 and first.error is None
    assert second.attempt == 2 and second.response is None
    assert isinstance(second.error, httpx2.ConnectError)


def test_a_policy_that_stops_leaves_the_request_s_own_error(mock_client):
    handler = replies(httpx2.Response(503))
    with pytest.raises(ApiError, match="HTTP 503") as raised:
        mock_client(handler, retry=Recording()).request("GET", "/items/")
    assert raised.value.status == 503
    assert len(handler.calls) == 1


def test_a_policy_can_stop_with_its_own_message(mock_client):
    class Refuse:
        def wait(self, failure):
            raise ApiError(f"gave up on {failure.url}", url=failure.url)

    handler = replies(httpx2.Response(503))
    with pytest.raises(ApiError, match="gave up on http://api.test/api/items/"):
        mock_client(handler, retry=Refuse()).request("GET", "/items/")


def test_a_policy_can_retry_on_what_the_body_says(mock_client):
    # The point of the interface: a rule Retry's settings can't express.
    class WhileWarmingUp:
        def wait(self, failure: Failure):
            body = failure.response.json() if failure.response is not None else {}
            return 0.0 if body.get("detail") == "warming up" else None

    warming = httpx2.Response(404, json={"detail": "warming up"})
    handler = replies(warming, OK)
    assert (
        mock_client(handler, retry=WhileWarmingUp()).request("GET", "/x/").status_code
        == 200
    )
    assert len(handler.calls) == 2


@pytest.mark.parametrize(
    ("retry", "seen"), [(True, [True]), (None, [None]), (False, [])]
)
def test_the_call_s_retry_argument_reaches_the_policy_unless_it_is_false(
    mock_client, retry, seen
):
    handler = replies(httpx2.Response(503))
    policy = Recording()
    with pytest.raises(ApiError):
        mock_client(handler, retry=policy).request("POST", "/items/", retry=retry)
    assert [f.retry for f in policy.failures] == seen


def test_a_retry_after_of_zero_still_backs_off():
    # Guard (dlt #4036): Retry-After: 0 caused an instant retry loop there.
    wait = Retry(backoff=1.0).wait(
        Failure(
            1, "GET", "http://h/x", httpx2.Response(429, headers={"Retry-After": "0"})
        )
    )
    assert wait is not None and wait >= 0.8


def test_a_token_endpoint_s_failure_is_judged_by_the_policy_s_statuses():
    # The policy, not Client, decides: a 503 from the token endpoint is
    # retried by default, and not when the policy leaves 503 out.
    from api_explorer import Failure

    failure = Failure(1, "GET", "http://h/x", error=ApiError("refresh", status=503))
    assert Retry(backoff=0).wait(failure) == 0
    assert Retry(backoff=0, statuses=frozenset({429})).wait(failure) is None
    assert (
        Retry(backoff=0).wait(
            Failure(1, "GET", "http://h/x", error=ApiError("no token"))
        )
        is None
    )
