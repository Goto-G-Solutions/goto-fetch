"""The default Transport, on urllib3: mature, fast, and installed almost everywhere.

Measured against Httpx2Transport on sequential JSON pages, it sends about
1.4 times as many small pages a second and 2.5 times as many large ones,
with less memory (see benchmarks/transports.py).
"""

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from .http import (
    ConnectError,
    ConnectionDropped,
    Request,
    Response,
    Timeout,
    TimeoutError,
    TransportError,
)

if TYPE_CHECKING:
    import urllib3


class Urllib3Transport:
    """A Transport on a urllib3 PoolManager, which reuses connections per host.

    Pass a PoolManager of your own as `pool` for proxies (urllib3's
    ProxyManager), certificates or connection limits; it stays open when
    this transport closes, and is yours to close. urllib3's own retries are
    off: the Client's retry policy decides. Redirects are followed, up to
    10, and compressed replies are asked for and unpacked. Safe to share
    across threads.
    """

    def __init__(self, pool: "urllib3.PoolManager | None" = None) -> None:
        import urllib3

        self._urllib3 = urllib3
        self._owned = pool is None
        self.pool = pool or urllib3.PoolManager()
        from urllib3.util import make_headers

        # gzip and deflate, and brotli or zstd when their packages are there.
        self._accept_encoding = make_headers(accept_encoding=True)["accept-encoding"]

    def send(self, request: Request, timeout: Timeout) -> Response:
        """Send `request` and return the response, following redirects."""
        urllib3 = self._urllib3
        headers = urllib3.HTTPHeaderDict(request.headers.raw())
        headers.setdefault("Accept-Encoding", self._accept_encoding)
        try:
            answer = self.pool.request(
                request.method,
                str(request.url),
                body=request.content or None,
                headers=headers,
                timeout=urllib3.Timeout(
                    total=None, connect=timeout.connect, read=timeout.read
                ),
                # No retries of urllib3's own (the Client's policy decides),
                # but redirects followed: total=False would turn those off too.
                retries=urllib3.Retry(
                    total=None,
                    connect=0,
                    read=0,
                    status=0,
                    other=0,
                    redirect=10,
                    raise_on_redirect=False,
                ),
                redirect=True,
                preload_content=True,
                decode_content=True,
            )
        except urllib3.exceptions.HTTPError as exc:
            raise _translate(urllib3, exc) from exc
        return Response(
            answer.status,
            headers=list(answer.headers.items()),
            content=answer.data,
            request=request,
            url=_final_url(str(request.url), answer),
        )

    def close(self) -> None:
        """Close the pool's connections, unless the pool was passed in."""
        if self._owned:
            self.pool.clear()


def _final_url(sent: str, answer: "urllib3.BaseHTTPResponse") -> str:
    """The absolute URL the response came from, after any redirects.

    urllib3 gives the last Location as written, often a bare path, so each
    hop is joined onto the URL before it.
    """
    url = sent
    retries = answer.retries
    for hop in retries.history if retries is not None else ():
        if hop.redirect_location:
            url = urljoin(url, hop.redirect_location)
    return url


def _translate(urllib3: Any, exc: Exception) -> TransportError:
    """urllib3's error as one of ours, by meaning: worth retrying or not.

    A refused connection or a failed name lookup is a ConnectError (urllib3
    files those under its connect timeouts), a timeout a TimeoutError, a
    reply cut short ConnectionDropped, and a TLS failure, which retrying
    won't fix, a plain TransportError.
    """
    errors = urllib3.exceptions
    reason = getattr(exc, "reason", None) or exc
    message = str(reason)
    if isinstance(reason, errors.SSLError):
        return TransportError(message)
    if isinstance(reason, errors.NewConnectionError):
        return ConnectError(message)
    if isinstance(reason, errors.TimeoutError):
        return TimeoutError(message)
    if isinstance(reason, errors.ProtocolError):
        return ConnectionDropped(message)
    if isinstance(exc, errors.MaxRetryError):
        return ConnectError(message)
    return TransportError(message)
