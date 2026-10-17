"""ApiError, and keeping secrets in URLs out of messages and logs."""

import io
import logging

import pytest

from api_explorer import ApiError, Retry, logging_to, redact_url
from api_explorer.testing import replies


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
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    client = replies(503).client(retry=Retry(attempts=2, backoff=0))
    with (
        logging_to(logging.StreamHandler(stream), "DEBUG", http=True),
        logging_to(Keep(), "DEBUG"),
        pytest.raises(ApiError),
    ):
        client.request("GET", "/x/?api_key=S3")
    assert "api_key=***" in stream.getvalue()
    assert "S3" not in stream.getvalue()  # httpx2's request lines included
    assert all("S3" not in str(r.data) for r in records if hasattr(r, "data"))
