"""Client: URLs, responses, errors and JSON decoding. Retries are in test_retry, paging in test_paging."""

import gzip
import json

import httpx2
import pytest

from api_explorer import ApiError, Client
from api_explorer.testing import replies

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
    assert mock_client(replies(OK)).url(endpoint) == url


def test_the_trailing_slash_can_be_turned_off(mock_client):
    client = mock_client(replies(OK), trailing_slash=False)
    assert client.url("/items") == "http://api.test/api/items"


def test_request_returns_the_response_on_success(mock_client):
    handler = replies(OK)
    assert mock_client(handler).request("GET", "/items/").json() == {"ok": True}
    assert str(handler.calls[0].url) == "http://api.test/api/items/"


def test_a_client_error_fails_at_once_with_its_status_and_body(mock_client):
    handler = replies(httpx2.Response(404, text="Not found."))
    with pytest.raises(ApiError, match="HTTP 404: Not found.") as raised:
        mock_client(handler).request("GET", "/items/1/")
    assert raised.value.status == 404
    assert len(handler.calls) == 1


def test_get_json_refuses_a_body_that_is_not_json(mock_client):
    handler = replies(httpx2.Response(200, text="<html>"))
    with pytest.raises(ApiError, match="did not return JSON"):
        mock_client(handler).get_json("/items/")


def test_get_json_parses_with_the_client_s_decoder(mock_client):
    handler = replies(httpx2.Response(200, content=b"raw body"))
    client = mock_client(handler, decode=lambda body: {"decoded": body})
    assert client.get_json("/items/") == {"decoded": b"raw body"}


def test_timeout_can_set_connect_and_read_limits_apart(mock_client):
    seen = {}

    def handler(request):
        seen.update(request.extensions["timeout"])
        return OK

    client = mock_client(handler, timeout=httpx2.Timeout(60.0, connect=5.0))
    client.request("GET", "/items/")
    assert (seen["connect"], seen["read"]) == (5.0, 60.0)


def test_a_client_you_bring_supplies_the_base_url_and_headers():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("X-Team")))
        return OK

    http = httpx2.Client(
        base_url="http://own.test/api",
        headers={"X-Team": "data"},
        transport=httpx2.MockTransport(handler),
    )
    Client(http=http).request("GET", "/items/")
    assert seen == [("http://own.test/api/items/", "data")]


@pytest.mark.parametrize(
    "setting",
    [
        {"base_url": "http://h"},
        {"headers": {"a": "b"}},
        {"timeout": 5.0},
        {"transport": httpx2.MockTransport(lambda r: OK)},
    ],
    ids=["base_url", "headers", "timeout", "transport"],
)
def test_a_setting_given_twice_is_refused(setting):
    # One place for each setting: here, or on the httpx2.Client, not both.
    with pytest.raises(
        ValueError, match=f"Set {next(iter(setting))} on the httpx2.Client"
    ):
        Client(http=httpx2.Client(), **setting)


def test_closing_leaves_a_client_you_bring_open_and_closes_its_own():
    yours = httpx2.Client()
    Client(http=yours).close()
    assert not yours.is_closed
    own = Client("http://h")
    own.close()
    assert own._http.is_closed


def test_params_merge_into_a_query_the_endpoint_already_has(mock_client):
    # Guard (httpx #3433): httpx2 replaced the endpoint's query.
    handler = replies(OK)
    mock_client(handler).request("GET", "/items/?a=1", params={"b": "2"})
    assert dict(handler.calls[0].url.params) == {"a": "1", "b": "2"}


def test_a_gzip_body_without_content_encoding_is_unpacked(mock_client):
    # Guard (Airbyte #914): some servers gzip without saying so.
    body = gzip.compress(json.dumps({"ok": True}).encode())
    assert mock_client(replies(httpx2.Response(200, content=body))).get_json("/x/") == {
        "ok": True
    }


def test_a_body_that_is_not_json_says_what_it_was(mock_client):
    page = httpx2.Response(
        200, text="<html>login</html>", headers={"Content-Type": "text/html"}
    )
    with pytest.raises(
        ApiError, match=r"did not return JSON \(text/html\).*began b'<html>login"
    ) as raised:
        mock_client(replies(page)).get_json("/x/")
    assert raised.value.status == 200


@pytest.mark.parametrize(
    "setting", [{"timeout": 0}, {"headers": {}}], ids=["zero-timeout", "empty-headers"]
)
def test_an_empty_setting_beside_http_is_refused_too(setting):
    # Regression: falsy settings beside http= were dropped without a word.
    with pytest.raises(ValueError, match="on the httpx2.Client you pass as http"):
        Client(http=httpx2.Client(), **setting)


def test_a_corrupt_gzip_body_is_an_api_error():
    response = httpx2.Response(200, content=b"\x1f\x8bnot gzip at all")
    client = replies(response).client()
    with pytest.raises(ApiError, match="gzip body that won't unpack"):
        client.get_json("/x/")
