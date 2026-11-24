"""ApiError, and keeping secrets in URLs out of messages and logs."""

import io
import logging

import pytest
from conftest import Collect

from goto_fetch import ApiError, Retry, logging_to, redact_url
from goto_fetch.testing import replies


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("http://h/x?api_key=S3&page=2", "http://h/x?api_key=***&page=2"),
        (
            "http://h/x?access_token=T&client_secret=C",
            "http://h/x?access_token=***&client_secret=***",
        ),
        (
            "http://h/x?Signature=abc&X-Amz-Credential=d",
            "http://h/x?Signature=***&X-Amz-Credential=***",
        ),
        (
            "http://h/x?q=late%20orders&apikey=k",
            "http://h/x?q=late%20orders&apikey=***",
        ),
        ("http://h/x?access%5Ftoken=T", "http://h/x?access%5Ftoken=***"),
        ("http://h/x?page=2&flag", "http://h/x?page=2&flag"),
        ("http://h/x", "http://h/x"),
    ],
    ids=[
        "api-key",
        "oauth",
        "signed-url",
        "keeps-encoding",
        "encoded-name",
        "nothing-secret",
        "no-query",
    ],
)
def test_redact_url_hides_only_sensitive_values_and_keeps_the_rest_exact(url, safe):
    assert redact_url(url) == safe


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://ghp_S3@h/x", "https://***@h/x"),
        (
            "https://h/cb#access_token=S3&state=1",
            "https://h/cb#access_token=***&state=1",
        ),
        ("https://h/docs#section-2", "https://h/docs#section-2"),
    ],
    ids=["lone-username", "fragment", "plain-fragment"],
)
def test_redact_url_hides_a_lone_username_and_secrets_in_a_fragment(url, safe):
    # Regression: a token used as the username, and an OAuth token in the
    # fragment, came back as written.
    assert redact_url(url) == safe


@pytest.mark.parametrize("url", ["http://[bad/x?token=S3", "https://S3@h:abc/x"])
def test_a_url_that_cant_be_read_is_hidden_whole(url):
    # Regression: redact_url raised ValueError on these, so an ApiError
    # naming one raised while being built and hid the real failure.
    assert redact_url(url) == "***"
    assert "S3" not in str(ApiError(f"GET {url} failed", url=url))


def test_api_errors_redact_their_message_and_url():
    # Regression: an API key in the query string appeared in errors.
    error = ApiError("GET http://h/x?api_key=S3 failed", url="http://h/x?api_key=S3")
    assert "S3" not in str(error)
    assert error.url == "http://h/x?api_key=***"


def test_a_failed_request_with_a_key_in_its_query_never_shows_the_key():
    with pytest.raises(ApiError) as raised:
        replies(503).client(retry=Retry(attempts=2, backoff=0)).request(
            "GET", "/x/?token=S3"
        )
    assert "S3" not in str(raised.value)
    assert "S3" not in raised.value.url


def test_log_lines_and_their_data_never_show_the_key():
    stream = io.StringIO()
    collect = Collect()
    client = replies(503).client(retry=Retry(attempts=2, backoff=0))
    with (
        logging_to(logging.StreamHandler(stream), "DEBUG", http=True),
        logging_to(collect, "DEBUG"),
        pytest.raises(ApiError),
    ):
        client.request("GET", "/x/?api_key=S3")
    assert "api_key=***" in stream.getvalue()
    assert "S3" not in stream.getvalue()  # the request events included
    assert all("S3" not in str(data) for _, data in collect.events())


def test_a_password_written_into_a_url_is_hidden():
    # Regression: redaction looked only at the query, so user:password@host
    # reached errors and log lines as written.
    assert redact_url("https://me:hunter2@h/x?page=2") == "https://me:***@h/x?page=2"
    assert "hunter2" not in str(ApiError("GET https://me:hunter2@h/x failed"))
