"""A Transport on httpx2, for HTTP/2, an httpx2.Client of your own, or httpx2's auth.

Needs the "httpx2" extra.
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
    import httpx2


class Httpx2Transport:
    """A Transport on an httpx2.Client.

    Pass a client of your own as `http` for what httpx2 offers: HTTP/2,
    proxies, certificates, event hooks, or an httpx2.Auth (set on that
    client). A client you pass stays open when this transport closes; it
    is yours to close. Redirects are followed.
    """

    def __init__(self, http: "httpx2.Client | None" = None) -> None:
        try:
            import httpx2
        except ImportError as exc:
            raise ImportError(
                "Httpx2Transport needs httpx2: install the extra, api-explorer[httpx2]"
            ) from exc
        self._httpx2 = httpx2
        self._owned = http is None
        self.http = http or httpx2.Client(follow_redirects=True)

    def send(self, request: Request, timeout: float | None) -> Response:
        """Send `request` and return the response."""
        httpx2 = self._httpx2
        try:
            answer = self.http.request(
                request.method,
                str(request.url),
                content=request.content or None,
                headers=request.headers.raw(),
                timeout=timeout,
            )
        except httpx2.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx2.RemoteProtocolError as exc:
            raise ConnectionDropped(str(exc)) from exc
        except httpx2.NetworkError as exc:
            raise ConnectError(str(exc)) from exc
        except httpx2.HTTPError as exc:
            raise TransportError(str(exc)) from exc
        return Response(
            answer.status_code,
            headers=list(answer.headers.multi_items()),
            content=answer.content,
            request=request,
            url=str(answer.url),
        )

    def close(self) -> None:
        """Close the httpx2 client, unless it was passed in."""
        if self._owned:
            self.http.close()
