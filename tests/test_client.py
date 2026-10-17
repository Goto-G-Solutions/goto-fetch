"""Client: URLs, responses, errors and JSON decoding. Retries are in test_retry, paging in test_paging."""

import httpx2
import pytest
from support import replies

from api_explorer import ApiError

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


def test_get_json_refuses_a_body_that_is_not_json(mock_client):
    handler, _ = replies(httpx2.Response(200, text="<html>"))
    with pytest.raises(ApiError, match="did not return JSON"):
        mock_client(handler).get_json("/items/")


def test_get_json_parses_with_the_client_s_decoder(mock_client):
    handler, _ = replies(httpx2.Response(200, content=b"raw body"))
    client = mock_client(handler, decode=lambda body: {"decoded": body})
    assert client.get_json("/items/") == {"decoded": b"raw body"}
