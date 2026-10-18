"""Client: URLs, responses, errors and JSON decoding. Retries are in test_retry, paging in test_paging."""

import gzip
import json

import pytest

from api_explorer import ApiError, Client, http
from api_explorer.testing import replies

OK = http.Response(200, json={"ok": True})


@pytest.mark.parametrize(
    ("endpoint", "url"),
    [
        ("/items", "http://api.test/api/items/"),
        ("items?page=2", "http://api.test/api/items/?page=2"),
        ("http://other.test/x?a=1", "http://other.test/x?a=1"),
    ],
)
def test_endpoints_join_the_base_url_with_a_trailing_slash(mock_client, endpoint, url):
    assert mock_client(replies(OK)).url(endpoint) == url


def test_the_trailing_slash_can_be_turned_off(mock_client):
    client = mock_client(replies(OK), trailing_slash=False)
    assert client.url("/items") == "http://api.test/api/items"


def test_request_returns_the_response_on_success(mock_client):
    handler = replies(OK)
    assert mock_client(handler).request("GET", "/items/").json() == {"ok": True}
    assert str(handler.calls[0].url) == "http://api.test/api/items/"


def test_a_client_error_fails_at_once_with_its_status_and_body(mock_client):
    handler = replies(http.Response(404, text="Not found."))
    with pytest.raises(ApiError, match="HTTP 404: Not found.") as raised:
        mock_client(handler).request("GET", "/items/1/")
    assert raised.value.status == 404
    assert len(handler.calls) == 1


def test_get_json_refuses_a_body_that_is_not_json(mock_client):
    handler = replies(http.Response(200, text="<html>"))
    with pytest.raises(ApiError, match="did not return JSON"):
        mock_client(handler).get_json("/items/")


def test_get_json_parses_with_the_client_s_decoder(mock_client):
    handler = replies(http.Response(200, content=b"raw body"))
    client = mock_client(handler, decode=lambda body: {"decoded": body})
    assert client.get_json("/items/") == {"decoded": b"raw body"}


class Recording:
    """A transport that answers OK and remembers each request and timeout."""

    def __init__(self):
        self.sent, self.closed = [], False

    def send(self, request, timeout):
        self.sent.append((request, timeout))
        return http.Response(200, json={"ok": True}, request=request)

    def close(self):
        self.closed = True


def test_the_timeout_and_every_request_go_through_the_transport():
    transport = Recording()
    Client("http://h/api", timeout=7.5, transport=transport).get_json("/items/")
    [(request, timeout)] = transport.sent
    assert (str(request.url), timeout) == ("http://h/api/items/", 7.5)


def test_client_headers_and_a_call_s_headers_both_reach_the_request():
    transport = Recording()
    client = Client("http://h", headers={"X-Team": "data"}, transport=transport)
    client.request("GET", "/x/", headers={"X-Trace": "1"})
    headers = transport.sent[0][0].headers
    assert (headers["x-team"], headers["X-Trace"]) == ("data", "1")


def test_closing_leaves_a_transport_you_pass_open_and_closes_its_own():
    yours = Recording()
    Client("http://h", transport=yours).close()
    assert not yours.closed
    own = Client("http://h")
    own.close()  # its Urllib3Transport releases its pool


def test_params_merge_into_a_query_the_endpoint_already_has(mock_client):
    # Guard (httpx #3433): httpx 0.28 replaced the endpoint's query.
    handler = replies(OK)
    mock_client(handler).request("GET", "/items/?a=1", params={"b": "2"})
    assert dict(handler.calls[0].url.params) == {"a": "1", "b": "2"}


def test_a_gzip_body_without_content_encoding_is_unpacked(mock_client):
    # Guard (Airbyte #914): some servers gzip without saying so.
    body = gzip.compress(json.dumps({"ok": True}).encode())
    assert mock_client(replies(http.Response(200, content=body))).get_json("/x/") == {
        "ok": True
    }


def test_a_body_that_is_not_json_says_what_it_was(mock_client):
    page = http.Response(
        200, text="<html>login</html>", headers={"Content-Type": "text/html"}
    )
    with pytest.raises(
        ApiError, match=r"did not return JSON \(text/html\).*began b'<html>login"
    ) as raised:
        mock_client(replies(page)).get_json("/x/")
    assert raised.value.status == 200


def test_a_corrupt_gzip_body_is_an_api_error():
    response = http.Response(200, content=b"\x1f\x8bnot gzip at all")
    client = replies(response).client()
    with pytest.raises(ApiError, match="gzip body that won't unpack"):
        client.get_json("/x/")
