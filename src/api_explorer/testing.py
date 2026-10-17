"""Fake APIs for tests: in-memory servers that page, fail and authenticate like real ones.

Each builder returns a FakeApi: a handler for httpx2.MockTransport that also
remembers every request it gets. fake.client() gives a Client wired to it,
with retries that don't wait. Nothing touches the network, and no test
framework is needed.

    api = flaky(offset_api(250, cap=100), 503)   # one failure, then pages
    records = list(api.client().records("/items/", results_key="items",
                                        paging=Offset(limit=1000)))
    assert records == list(range(250))
    assert len(api.calls) == 4                   # the 503, then 3 pages
"""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import httpx2

from .client import Client
from .retry import Retry

Answer = Callable[[httpx2.Request], httpx2.Response]
"""Turns one request into a response, as httpx2.MockTransport expects."""


class FakeApi:
    """A fake API: answers requests in memory, and remembers each one.

    Pass it to httpx2.MockTransport, or call client() for a Client wired to
    it. `calls` holds every request in order; `paths`, `queries` and `bodies`
    read the same requests in the forms tests usually check.
    """

    def __init__(self, answer: Answer) -> None:
        self._answer = answer
        self.calls: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        return self._answer(request)

    @property
    def paths(self) -> list[str]:
        """The path of each request, such as "/api/items/"."""
        return [request.url.path for request in self.calls]

    @property
    def queries(self) -> list[dict[str, str]]:
        """The query parameters of each request."""
        return [dict(request.url.params) for request in self.calls]

    @property
    def bodies(self) -> list[Any]:
        """The JSON body of each request, or None for a request without one."""
        return [
            json.loads(request.content) if request.content else None
            for request in self.calls
        ]

    def client(self, base_url: str = "http://api.test/api", **options: Any) -> Client:
        """A Client whose requests come here. Retries don't wait, unless `retry` says otherwise."""
        options.setdefault("retry", Retry(backoff=0))
        return Client(base_url, transport=httpx2.MockTransport(self), **options)


def _records(records: Sequence[Any] | int) -> list[Any]:
    return list(range(records)) if isinstance(records, int) else list(records)


def _query(request: httpx2.Request, name: str, default: int) -> int:
    return int(request.url.params.get(name, default))


def replies(*responses: httpx2.Response | int | Exception) -> FakeApi:
    """Answer requests with `responses`, in order; the last one repeats.

    Each is an httpx2.Response, a status code, or an exception to raise,
    such as httpx2.ConnectError("refused").
    """
    if not responses:
        raise ValueError("replies needs at least one response")
    count = 0

    def answer(request: httpx2.Request) -> httpx2.Response:
        nonlocal count
        count += 1
        return _respond(responses[min(count, len(responses)) - 1])

    return FakeApi(answer)


def _respond(reply: httpx2.Response | int | Exception) -> httpx2.Response:
    if isinstance(reply, Exception):
        raise reply
    if isinstance(reply, int):
        return httpx2.Response(reply)
    return reply


def json_pages(pages: Mapping[str, Any]) -> FakeApi:
    """Serve a JSON body for each full URL in `pages`, and 404 for anything else."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        return (
            httpx2.Response(200, json=pages[url])
            if url in pages
            else httpx2.Response(404)
        )

    return FakeApi(answer)


def next_link_api(
    records: Sequence[Any] | int, *, page_size: int = 100, results_key: str = "results"
) -> FakeApi:
    """Django REST Framework style: {"count": N, "next": URL or null, "results": [...]}.

    `records` is a list, or a number n for the records 0 to n-1. Pages are
    chosen by ?page=, starting at 1.
    """
    items = _records(records)

    def answer(request: httpx2.Request) -> httpx2.Response:
        page = _query(request, "page", 1)
        start = (page - 1) * page_size
        more = start + page_size < len(items)
        following = str(request.url.copy_set_param("page", page + 1)) if more else None
        body = {
            "count": len(items),
            "next": following,
            results_key: items[start : start + page_size],
        }
        return httpx2.Response(200, json=body)

    return FakeApi(answer)


def offset_api(
    records: Sequence[Any] | int,
    *,
    cap: int | None = None,
    count: bool | int = True,
    ignore_offset: bool = False,
    results_key: str = "items",
    limit_param: str = "limit",
    offset_param: str = "offset",
) -> FakeApi:
    """Django Ninja style: ?limit=&offset=, answered with {"items": [...], "count": N}.

    `cap` limits the page size whatever the client asks, as many servers
    do. `count` is True for the real total, False to leave it out, or a
    number to report instead. With `ignore_offset`, every page starts at 0,
    like a server that doesn't support offsets.
    """
    items = _records(records)

    def answer(request: httpx2.Request) -> httpx2.Response:
        limit = _query(request, limit_param, len(items) or 1)
        offset = 0 if ignore_offset else _query(request, offset_param, 0)
        size = min(limit, cap) if cap else limit
        body: dict[str, Any] = {results_key: items[offset : offset + size]}
        if count is not False:
            body["count"] = len(items) if count is True else count
        return httpx2.Response(200, json=body)

    return FakeApi(answer)


def cursor_api(
    records: Sequence[Any] | int,
    *,
    page_size: int = 100,
    send_in: Literal["query", "body"] = "query",
    results_key: str = "data",
    cursor_key: str = "next_cursor",
    param: str = "cursor",
) -> FakeApi:
    """A cursor API: each page names the cursor for the next, until the last.

    The client sends the cursor back as `param`, in the query string or, with
    send_in="body", in a JSON body, as POST search APIs do. Pages look like
    {"data": [...], "next_cursor": "100"}; the last has a null cursor.
    """
    items = _records(records)

    def answer(request: httpx2.Request) -> httpx2.Response:
        if send_in == "body":
            body = json.loads(request.content) if request.content else {}
            start = int(body.get(param) or 0)
        else:
            start = _query(request, param, 0)
        more = start + page_size < len(items)
        page = {
            results_key: items[start : start + page_size],
            cursor_key: str(start + page_size) if more else None,
        }
        return httpx2.Response(200, json=page)

    return FakeApi(answer)


def link_header_api(records: Sequence[Any] | int, *, page_size: int = 100) -> FakeApi:
    """GitHub style: the body is a bare list, and a Link header points to the next page.

    Pages are chosen by ?page=, starting at 1; the last page has no Link.
    """
    items = _records(records)

    def answer(request: httpx2.Request) -> httpx2.Response:
        page = _query(request, "page", 1)
        start = (page - 1) * page_size
        headers = {}
        if start + page_size < len(items):
            headers["Link"] = (
                f'<{request.url.copy_set_param("page", page + 1)}>; rel="next"'
            )
        return httpx2.Response(
            200, json=items[start : start + page_size], headers=headers
        )

    return FakeApi(answer)


def flaky(
    api: FakeApi | Answer, *failures: httpx2.Response | int | Exception
) -> FakeApi:
    """Fail the first requests as told, then let `api` answer the rest.

    Each failure is a status code (such as 503), an httpx2.Response (such as
    a 429 with a Retry-After header), or an exception to raise (such as
    httpx2.ReadTimeout("slow")). The result is a FakeApi too, so it composes:
    flaky(flaky(api, 503), 429).
    """
    remaining = list(failures)

    def answer(request: httpx2.Request) -> httpx2.Response:
        if remaining:
            return _respond(remaining.pop(0))
        return api(request)

    return FakeApi(answer)


class TokenServer(FakeApi):
    """A simplejwt-style API: log in, refresh, and fetch with a bearer token.

    POST `login_path` with {"username": ..., "password": ...} gives
    {"access": ..., "refresh": ...}. POST `refresh_path` with
    {"refresh": ...} gives a new access token, and with `rotate`, a new
    refresh token that replaces the old one. Any other path wants
    "Authorization: Bearer <access>" and answers {"results": [...],
    "next": null}: `records`, or a request's own JSON body as the one
    record, so a test can see what was sent.

    The refresh token "r0" works from the start. `access_tokens` and
    `refresh_tokens` are the tokens it accepts; expire_access_tokens()
    makes every access token stale, as time passing would.
    """

    def __init__(
        self,
        *,
        records: Sequence[Any] = ({"id": 1},),
        username: str = "me",
        password: str = "pw",
        login_path: str = "/api/token/",
        refresh_path: str = "/api/token/refresh/",
        access_field: str = "access",
        rotate: bool = False,
        accept_logins: bool = True,
        deny_all: bool = False,
    ) -> None:
        self.access_tokens: set[str] = set()
        self.refresh_tokens: set[str] = {"r0"}
        self._issued = 0

        def issue(prefix: str) -> str:
            self._issued += 1
            return f"{prefix}{self._issued}"

        def answer(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content) if request.content else None
            path = request.url.path
            if path == login_path:
                if not accept_logins or body != {
                    "username": username,
                    "password": password,
                }:
                    return httpx2.Response(401, json={"detail": "No active account"})
                access, refresh = issue("a"), issue("r")
                self.access_tokens.add(access)
                self.refresh_tokens.add(refresh)
                return httpx2.Response(
                    200, json={access_field: access, "refresh": refresh}
                )
            if path == refresh_path:
                old = (body or {}).get("refresh")
                if old not in self.refresh_tokens:
                    return httpx2.Response(401, json={"detail": "Token is invalid"})
                access = issue("a")
                self.access_tokens.add(access)
                reply = {access_field: access}
                if rotate:
                    self.refresh_tokens.discard(old)
                    reply["refresh"] = issue("r")
                    self.refresh_tokens.add(reply["refresh"])
                return httpx2.Response(200, json=reply)
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if deny_all or token not in self.access_tokens:
                return httpx2.Response(401, json={"detail": "Not authenticated"})
            results = [body] if body is not None else list(records)
            return httpx2.Response(200, json={"results": results, "next": None})

        super().__init__(answer)

    def expire_access_tokens(self) -> None:
        """Make every access token stale, so the next request needs a new one."""
        self.access_tokens.clear()
