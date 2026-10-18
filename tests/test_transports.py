"""Transports: the built-in ones keep every Transport promise, and the check catches broken ones.

check_transport runs a fake API behind a real server on 127.0.0.1, so
these tests send real HTTP, but never beyond this machine.
"""

import pytest

from api_explorer import Client, Httpx2Transport, Urllib3Transport, http
from api_explorer.testing import FakeApi, check_transport, next_link_api, serve


def test_the_urllib3_transport_keeps_every_transport_promise():
    check_transport(Urllib3Transport)


def test_the_httpx2_transport_keeps_every_transport_promise():
    pytest.importorskip("httpx2")
    check_transport(Httpx2Transport)


def test_a_fake_api_keeps_the_shape_of_a_transport():
    assert isinstance(FakeApi(lambda request: http.Response(200)), http.Transport)


def test_a_client_pages_over_real_http_with_either_transport():
    pytest.importorskip("httpx2")
    with serve(next_link_api(25, page_size=10)) as base:
        for transport in (Urllib3Transport(), Httpx2Transport()):
            with Client(f"{base}/api", transport=transport) as client:
                assert list(client.records("/items/")) == list(range(25))
            transport.close()


def test_an_httpx2_client_of_your_own_keeps_its_settings():
    httpx2 = pytest.importorskip("httpx2")
    with serve(
        lambda r: http.Response(200, json={"team": r.headers.get("X-Team")})
    ) as base:
        own = httpx2.Client(headers={"X-Team": "data"}, follow_redirects=True)
        transport = Httpx2Transport(http=own)
        assert Client(base, transport=transport).get_json("/x/") == {"team": "data"}
        transport.close()
        assert not own.is_closed  # yours to close
        own.close()


class RaisesOnErrors(Urllib3Transport):
    """Broken: raises for an error status instead of returning it."""

    def send(self, request, timeout):
        response = super().send(request, timeout)
        if response.status_code >= 400:
            raise http.TransportError(f"HTTP {response.status_code}")
        return response


class NeverWaits(Urllib3Transport):
    """Broken: ignores the timeout, so a slow server hangs the run."""

    def send(self, request, timeout):
        return super().send(request, None)


class StopsAtRedirects(Urllib3Transport):
    """Broken: returns a redirect instead of following it."""

    def send(self, request, timeout):
        answer = self.pool.request(
            request.method,
            str(request.url),
            body=request.content or None,
            headers=dict(request.headers),
            redirect=False,
            retries=False,
        )
        return http.Response(
            answer.status,
            headers=list(answer.headers.items()),
            content=answer.data,
            request=request,
        )


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        (RaisesOnErrors, "a 404 came back as"),
        (NeverWaits, "a timeout didn't raise NetworkError"),
        (StopsAtRedirects, "a redirect wasn't followed"),
    ],
    ids=["raises-on-errors", "never-waits", "stops-at-redirects"],
)
def test_check_transport_catches_a_broken_transport(broken, message):
    with pytest.raises(AssertionError, match=message):
        check_transport(broken)
