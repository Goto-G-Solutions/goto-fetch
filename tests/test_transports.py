"""Transports: the built-in ones keep every Transport promise, and the check catches broken ones.

check_transport runs a fake API behind a real server on 127.0.0.1, so
these tests send real HTTP, but never beyond this machine.
"""

import gzip

import pytest

from goto_fetch import Client, Httpx2Transport, Urllib3Transport, http
from goto_fetch.testing import check_transport, next_link_api, serve


def test_the_urllib3_transport_keeps_every_transport_promise():
    check_transport(Urllib3Transport)


def test_the_httpx2_transport_keeps_every_transport_promise():
    pytest.importorskip("httpx2")
    check_transport(Httpx2Transport)


def test_an_httpx2_client_of_your_own_still_keeps_every_promise():
    # Regression: one made without follow_redirects=True returned redirects.
    httpx2 = pytest.importorskip("httpx2")
    own = httpx2.Client()
    check_transport(lambda: Httpx2Transport(http=own))
    own.close()


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
        own = httpx2.Client(headers={"X-Team": "data"})
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


class RaisesOnServerErrors(Urllib3Transport):
    """Broken: returns a 404 but raises for a 5xx, which retries need returned."""

    def send(self, request, timeout):
        response = super().send(request, timeout)
        if response.status_code >= 500:
            raise http.TransportError(f"HTTP {response.status_code}")
        return response


class LeavesGzipPacked(Urllib3Transport):
    """Broken: hands back a gzip body still packed."""

    def send(self, request, timeout):
        response = super().send(request, timeout)
        if response.headers.get("Content-Encoding") == "gzip":
            response.content = gzip.compress(response.content)
        return response


class AnyErrorWillDo(Urllib3Transport):
    """Broken: every failure is a plain TransportError, so nothing is retried."""

    def send(self, request, timeout):
        try:
            return super().send(request, timeout)
        except http.TransportError as exc:
            raise http.TransportError(str(exc)) from exc


class DropsHeaders(Urllib3Transport):
    """Broken: sends no headers of the caller's."""

    def send(self, request, timeout):
        bare = http.Request(request.method, str(request.url), content=request.content)
        return super().send(bare, timeout)


class NeverWaits(Urllib3Transport):
    """Broken: ignores the timeout, so a slow server hangs the run."""

    def send(self, request, timeout):
        return super().send(request, http.Timeout())  # no limits at all


class OneLimit(Urllib3Transport):
    """Broken: one limit for both stages, the shorter, as a total timeout would be."""

    def send(self, request, timeout):
        shorter = min(timeout.connect, timeout.read)
        return super().send(request, http.Timeout(shorter, shorter))


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
        (RaisesOnServerErrors, "a 503 came back as an error"),
        (LeavesGzipPacked, "a gzip body came back packed"),
        (AnyErrorWillDo, "raised TransportError, not TimeoutError"),
        (DropsHeaders, "headers or body didn't arrive as sent"),
        (NeverWaits, "a read slower than the read limit raised nothing"),
        (OneLimit, "a short connect timeout cut a slow read short"),
        (StopsAtRedirects, "a redirect wasn't followed"),
    ],
    ids=[
        "raises-on-errors",
        "raises-on-server-errors",
        "leaves-gzip-packed",
        "any-error-will-do",
        "drops-headers",
        "never-waits",
        "one-limit",
        "stops-at-redirects",
    ],
)
def test_check_transport_catches_a_broken_transport(broken, message):
    with pytest.raises(AssertionError, match=message):
        check_transport(broken)
