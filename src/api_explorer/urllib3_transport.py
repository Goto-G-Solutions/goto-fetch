"""The default Transport, on urllib3: mature, fast, and installed almost everywhere.

Measured against Httpx2Transport on sequential JSON pages, it sends about
1.4 times as many small pages a second and 2.5 times as many large ones,
with less memory (see benchmarks/transports.py).
"""

from typing import TYPE_CHECKING

from .http import (
    ConnectError,
    ConnectionDropped,
    Request,
    Response,
    TimeoutError,
    TransportError,
)

if TYPE_CHECKING:
    import urllib3


class Urllib3Transport:
    """A Transport on a urllib3 PoolManager, which reuses connections per host.

    Pass a PoolManager of your own as `pool` for proxies (urllib3's
    ProxyManager), certificates or connection limits. urllib3's own retries
    are off: the Client's retry policy decides. Redirects are followed, up
    to 10. Safe to share across threads.
    """

    def __init__(self, pool: "urllib3.PoolManager | None" = None) -> None:
        import urllib3

        self._urllib3 = urllib3
        self.pool = pool or urllib3.PoolManager()

    def send(self, request: Request, timeout: float | None) -> Response:
        """Send `request` and return the response, following redirects."""
        urllib3 = self._urllib3
        try:
            answer = self.pool.request(
                request.method,
                str(request.url),
                body=request.content or None,
                headers=dict(request.headers),
                timeout=urllib3.Timeout(total=None, connect=timeout, read=timeout),
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
        except urllib3.exceptions.TimeoutError as exc:
            raise TimeoutError(str(exc)) from exc
        except urllib3.exceptions.ProtocolError as exc:
            raise ConnectionDropped(str(exc)) from exc
        except (
            urllib3.exceptions.NewConnectionError,
            urllib3.exceptions.MaxRetryError,
        ) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, urllib3.exceptions.TimeoutError):
                raise TimeoutError(str(exc)) from exc
            if isinstance(reason, urllib3.exceptions.ProtocolError):
                raise ConnectionDropped(str(exc)) from exc
            raise ConnectError(str(exc)) from exc
        except urllib3.exceptions.HTTPError as exc:
            raise TransportError(str(exc)) from exc
        return Response(
            answer.status,
            headers=list(answer.headers.items()),
            content=answer.data,
            request=request,
            url=answer.url or str(request.url),
        )

    def close(self) -> None:
        """Close the pool's connections."""
        self.pool.clear()
