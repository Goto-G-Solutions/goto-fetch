"""Fake APIs for tests, and checks for pieces you build on this package's interfaces.

Each builder returns a FakeApi: a handler for httpx2.MockTransport that also
remembers every request it gets. fake.client() gives a Client wired to it,
with retries that don't wait. Nothing touches the network, and no test
framework is needed.

    api = flaky(offset_api(250, cap=100), 503)   # one failure, then pages
    records = list(api.client().records("/items/", results_key="items",
                                        paging=Offset(limit=1000)))
    assert records == list(range(250))
    assert len(api.calls) == 4                   # the 503, then 3 pages

For pieces of your own: fetched() builds a page to test a paging style on
its own, and check_retry_policy(), check_paging(), check_state_store(), check_tracker()
and check_warehouse() check the promises every retry policy, paging style,
state store, tracker and warehouse must keep. MemoryState and
MemoryWarehouse never touch the disk.
"""

import base64
import json
import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any, Literal

import httpx2

from .client import Client
from .errors import ApiError, Problem
from .incremental import ReportsDeletions, Tracker
from .models import as_dict
from .paging import Fetched, PageRequest, Paging
from .retry import Failure, Retry, RetryPolicy
from .state import StateStore
from .warehouse import (
    Warehouse,
    check_write,
    deleted_keys,
    last_per_key,
    same_value,
)

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
    return FakeApi(
        lambda request: _next_link_page(request, items, page_size, results_key)
    )


def _next_link_page(
    request: httpx2.Request, items: Sequence[Any], page_size: int, results_key: str
) -> httpx2.Response:
    """Answer `request` with its ?page= of `items`, in next_link_api's form."""
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


def changes_api(
    records: list[dict[str, Any]],
    *,
    field: str = "updated_at",
    param: str = "updated_since",
    compare: Literal[">=", ">"] | None = ">=",
    page_size: int = 100,
) -> FakeApi:
    """An API that filters by a field's value, for testing Incremental: next_link_api's pages.

    ?`param`=value keeps the records whose `field` is >= value (or > value,
    with compare=">"; compare=None ignores the filter, like an API without
    one). Values compare as text, so give them one fixed format, such as
    "2026-01-02T10:00:00Z". The fake reads `records` on every request, so a
    test can add or change records between runs, as a real API's data does.
    """

    def answer(request: httpx2.Request) -> httpx2.Response:
        since = request.url.params.get(param)
        if since is None or compare is None:
            chosen = list(records)
        elif compare == ">=":
            chosen = [r for r in records if r[field] >= since]
        else:
            chosen = [r for r in records if r[field] > since]
        return _next_link_page(request, chosen, page_size, "results")

    return FakeApi(answer)


def keyset_api(
    records: list[dict[str, Any]],
    *,
    field: str = "updated_at",
    param: str = "updated_since",
    limit_param: str = "limit",
    cap: int | None = None,
    ignore_filter: bool = False,
) -> FakeApi:
    """An API sorted by a field, paged by "from this value on", for testing Keyset.

    It answers {"results": [...]}: the records whose `field` is >= the
    `param` value, sorted by `field` (ties keep their order in `records`),
    at most ?`limit_param`= of them (default 100), and at most `cap` if
    given, as servers with a largest page size do. With `ignore_filter`,
    every page starts from the first record. Values compare as text, so
    give them one fixed format. Like changes_api, it reads `records` on
    every request, so a test can change the data during a run.
    """

    def answer(request: httpx2.Request) -> httpx2.Response:
        since = request.url.params.get(param)
        size = _query(request, limit_param, 100)
        if cap is not None:
            size = min(size, cap)
        chosen = sorted(records, key=lambda r: r[field])
        if since is not None and not ignore_filter:
            chosen = [r for r in chosen if r[field] >= since]
        return httpx2.Response(200, json={"results": chosen[:size]})

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
    {"access": ..., "refresh": ...}. The same path also takes the OAuth2
    client credentials grant: a form with grant_type=client_credentials and
    the client's ID and secret (in a Basic header or the form), answered
    with {"access_token": ..., "token_type": "Bearer"}. POST `refresh_path` with
    {"refresh": ...} gives a new access token, and with `rotate`, a new
    refresh token that replaces the old one. Any other path wants
    "Authorization: Bearer <access>" and answers {"results": [...],
    "next": null}: `records`, or a request's own JSON body as the one
    record, so a test can see what was sent.

    The refresh token "r0" works from the start. `access_tokens` and
    `refresh_tokens` are the tokens it accepts; expire_access_tokens()
    makes every access token stale, as time passing would. With
    `expires_in`, every token reply says how many seconds it lasts.
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
        client_id: str = "client",
        client_secret: str = "secret",
        expires_in: int | None = None,
    ) -> None:
        self.access_tokens: set[str] = set()
        self.refresh_tokens: set[str] = {"r0"}
        self._issued = 0

        def issue(prefix: str) -> str:
            self._issued += 1
            return f"{prefix}{self._issued}"

        def lasting(reply: dict[str, Any]) -> dict[str, Any]:
            return reply if expires_in is None else {**reply, "expires_in": expires_in}

        def client_credentials(request: httpx2.Request) -> httpx2.Response:
            form = dict(httpx2.QueryParams(request.content.decode()))
            pair = f"{client_id}:{client_secret}".encode()
            basic = "Basic " + base64.b64encode(pair).decode()
            in_header = request.headers.get("Authorization") == basic
            in_body = (form.get("client_id"), form.get("client_secret")) == (
                client_id,
                client_secret,
            )
            if form.get("grant_type") != "client_credentials" or not (
                in_header or in_body
            ):
                return httpx2.Response(401, json={"error": "invalid_client"})
            access = issue("a")
            self.access_tokens.add(access)
            reply = {"access_token": access, "token_type": "Bearer"}
            return httpx2.Response(200, json=lasting(reply))

        def answer(request: httpx2.Request) -> httpx2.Response:
            content_type = request.headers.get("Content-Type", "")
            if request.url.path == login_path and content_type.startswith(
                "application/x-www-form-urlencoded"
            ):
                return client_credentials(request)
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
                    200, json=lasting({access_field: access, "refresh": refresh})
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
                return httpx2.Response(200, json=lasting(reply))
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if deny_all or token not in self.access_tokens:
                return httpx2.Response(401, json={"detail": "Not authenticated"})
            results = [body] if body is not None else list(records)
            return httpx2.Response(200, json={"results": results, "next": None})

        super().__init__(answer)

    def expire_access_tokens(self) -> None:
        """Make every access token stale, so the next request needs a new one."""
        self.access_tokens.clear()


def fetched(
    body: Any = None,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    url: str = "http://api.test/api/items/",
) -> Fetched:
    """Build the page a paging style receives, to test a style on its own.

        first, next_link = MyStyle().begin(PageRequest(url), read)
        assert next_link(fetched({"next": "?page=2"}), first) == "?page=2"

    `body` is the parsed JSON; `headers` (such as a Link header) and
    `status` go on the response, which came from GET `url`.
    """
    request = httpx2.Request("GET", url)
    response = httpx2.Response(status, json=body, headers=headers, request=request)
    return Fetched(body, response)


def check_retry_policy(policy: RetryPolicy, *, max_attempts: int = 100) -> None:
    """Check a retry policy keeps the promises every policy must keep.

    The policy is shown failures that never go away, of each kind: a 503, a
    429, a network error, a POST answered with a 503, and a 200 whose body
    reports a temporary problem. For each, it must
    give up (return None, or raise ApiError) within `max_attempts` tries,
    and every wait it asks for must be a number of seconds that is neither
    negative nor endless. Nothing sleeps, so this is quick.

    Raises AssertionError, saying which rule broke and how.
    """
    url = "http://api.test/api/items/"
    busy = Problem("busy", temporary=True)
    kinds: dict[
        str, tuple[str, httpx2.Response | None, Exception | None, Problem | None]
    ] = {
        "a 503": ("GET", httpx2.Response(503), None, None),
        "a 429": ("GET", httpx2.Response(429), None, None),
        "a network error": ("GET", None, httpx2.ConnectError("refused"), None),
        "a POST answered with 503": ("POST", httpx2.Response(503), None, None),
        "a 200 reporting a temporary problem": (
            "GET",
            httpx2.Response(200),
            None,
            busy,
        ),
    }
    for kind, (method, response, error, problem) in kinds.items():
        for attempt in range(1, max_attempts + 1):
            failure = Failure(attempt, method, url, response, error, problem=problem)
            try:
                wait = policy.wait(failure)
            except ApiError:
                break
            if wait is None:
                break
            seconds = isinstance(wait, (int, float)) and not isinstance(wait, bool)
            if not (seconds and 0 <= wait < math.inf):
                raise AssertionError(
                    f"after {kind}, the policy asked to wait {wait!r}; a wait must be "
                    "a number of seconds, neither negative nor endless"
                )
        else:
            raise AssertionError(
                f"after {kind} that never goes away, the policy was still retrying "
                f"after {max_attempts} tries; a policy must give up"
            )


def check_paging(
    paging: Paging,
    api: FakeApi,
    expected: Sequence[Any],
    *,
    endpoint: str = "/items/",
    results_key: str | None = "results",
    count_key: str | None = None,
    method: str = "GET",
    json: Any = None,
    max_requests: int | None = None,
) -> None:
    """Check a paging style gets every record from a fake API, once each, and stops.

    Runs Client.records against `api` with this style, and checks that:

    - it stops within `max_requests` requests (by default one per record,
      plus two: enough for any style that moves on by at least one record
      a page);
    - it never asks for a page it already fetched;
    - the records are exactly `expected`, in order, none missing and none
      twice.

    The remaining arguments go to Client.records. Raises AssertionError,
    saying which rule broke and how.
    """
    limit = len(expected) + 2 if max_requests is None else max_requests
    try:
        records = list(
            api.client().records(
                endpoint,
                method=method,
                json=json,
                results_key=results_key,
                count_key=count_key,
                paging=paging,
                max_pages=limit + 1,
            )
        )
    except ApiError as error:
        if "Pagination loops" in str(error):
            raise AssertionError(f"the paging style looped: {error}") from error
        raise
    if len(api.calls) > limit:
        raise AssertionError(
            f"the paging style didn't stop within {limit} requests "
            f"(it made {len(api.calls)})"
        )
    if records != list(expected):
        missing = [r for r in expected if r not in records]
        extra = [r for r in records if r not in expected]
        twice = [r for r in expected if records.count(r) > 1]
        problems = [
            f"missing {missing[:5]}" if missing else "",
            f"unexpected {extra[:5]}" if extra else "",
            f"repeated {twice[:5]}" if twice else "",
            "out of order" if not (missing or extra or twice) else "",
        ]
        raise AssertionError(
            "the paging style didn't fetch the expected records: "
            + "; ".join(p for p in problems if p)
        )


class MemoryState:
    """A StateStore in memory, for tests: nothing touches the disk.

    It keeps each value as JSON text, so like a real store it gives back
    copies, and refuses values that aren't plain JSON. `saves` counts the
    calls to put, so a test can check that a failed run saved nothing.
    """

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._text = {name: json.dumps(v) for name, v in (values or {}).items()}
        self.saves = 0

    def get(self, name: str) -> Any:
        """The value last saved under `name`, or None."""
        text = self._text.get(name)
        return None if text is None else json.loads(text)

    def put(self, name: str, value: Any) -> None:
        """Save `value` under `name`, replacing what was there."""
        self._text[name] = json.dumps(value, allow_nan=False)
        self.saves += 1


def check_state_store(store: StateStore) -> None:
    """Check a state store keeps the promises every StateStore must keep.

    Use names starting "check-state-store" in a store you don't mind it
    writing to. Raises AssertionError naming the first promise broken.
    """
    a, b = "check-state-store-a", "check-state-store-b"

    def fresh() -> dict[str, Any]:
        return {"last": "2026-01-02T10:00:00Z", "seen": ["1", "2"], "n": 3, "ok": True}

    value = fresh()
    if store.get(a) is not None or store.get(b) is not None:
        raise AssertionError(
            f"get must give None for a name never saved; {a!r} or {b!r} gave a "
            "value (or the store was used by an earlier check)"
        )
    store.put(a, value)
    got = store.get(a)
    if got != value:
        raise AssertionError(f"put {value!r} but get gave back {got!r}")
    got["seen"].append("changed")
    # Compare with a fresh value: `value` itself may be what the store kept.
    if store.get(a) != fresh():
        raise AssertionError("get gave back the stored object itself, not a copy")
    if store.get(b) is not None:
        raise AssertionError(f"putting {a!r} changed {b!r}")
    store.put(a, {"last": None, "seen": []})
    if store.get(a) != {"last": None, "seen": []}:
        raise AssertionError("put didn't replace the earlier value")


def check_tracker(
    make: Callable[[dict[str, Any] | None], Tracker], records: Sequence[Any]
) -> None:
    """Check a tracker keeps the promises every Tracker must keep.

    `make(state)` builds the tracker for a run from the last run's saved
    state, or None for the first run: for example
    lambda state: Incremental("updated_at", state=state). `records` are
    what the source holds, at least two, all different, with whatever
    fields the tracker reads. Raises AssertionError naming the first
    promise broken:

    - apply returns a PageRequest;
    - state is plain JSON, before and after a run;
    - a run that stops early, or whose source fails, leaves state as it
      was, and the failure reaches the caller;
    - a first run delivers every record, and only those given;
    - the next run, over the same records, delivers none.
    """
    if len(records) < 2:
        raise ValueError("check_tracker needs at least two records")
    request = PageRequest("http://api.test/api/items/")

    def plain(state: Any, when: str) -> dict[str, Any]:
        try:
            again = json.loads(json.dumps(state, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise AssertionError(f"state {when} isn't plain JSON: {exc}") from exc
        if again != state:
            raise AssertionError(
                f"state {when} changes in a JSON round trip: {state!r}"
            )
        return again

    tracker = make(None)
    applied = tracker.apply(request)
    returns_a_request = isinstance(applied, PageRequest)
    if not returns_a_request:
        raise AssertionError(f"apply must return a PageRequest, got {applied!r}")
    start = plain(tracker.state, "before a run")

    stream = tracker.track(iter(records))
    next(stream, None)
    close = getattr(stream, "close", None)
    if close is not None:
        close()
    if tracker.state != start:
        raise AssertionError("a run stopped early moved the state on")

    class SourceFailed(Exception):
        """The failure check_tracker's source raises part way."""

    def failing() -> Iterator[Any]:
        yield records[0]
        raise SourceFailed

    tracker = make(None)
    try:
        for _ in tracker.track(failing()):
            pass
    except SourceFailed:
        pass
    else:
        raise AssertionError("track hid a failure of its source")
    if tracker.state != start:
        raise AssertionError("a run whose source failed moved the state on")

    tracker = make(None)
    delivered = list(tracker.track(iter(records)))
    strangers = [r for r in delivered if not any(r is given for given in records)]
    if strangers:
        raise AssertionError(
            f"track delivered records it wasn't given: {strangers[:3]!r}"
        )
    missing = [r for r in records if not any(r is got for got in delivered)]
    if missing:
        raise AssertionError(
            f"a first run didn't deliver every record: missing {missing[:3]!r}"
        )
    saved = plain(tracker.state, "after a run")

    again = list(make(saved).track(iter(records)))
    if again:
        raise AssertionError(
            f"nothing changed, yet the next run delivered {len(again)} records: "
            f"{again[:3]!r}"
        )


class MemoryWarehouse:
    """A Warehouse in memory, for tests: nothing touches the disk, nothing to install.

    It keeps every promise of Warehouse, and stores values exactly as
    written (it never needs to widen a column). It is also a StateStore,
    like the database warehouses, so tracked_run can keep state in it.
    `writes` counts the writes that were stored.
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[str]] = {}
        self._state = MemoryState()
        self.writes = 0

    def write(
        self,
        table: str,
        records: Iterable[Any],
        *,
        key: str | None = None,
        deleted_by: ReportsDeletions | None = None,
    ) -> int:
        """Store `records` in `table`, all or nothing; return how many were stored."""
        check_write(key, deleted_by, 1)
        rows = [json.loads(json.dumps(as_dict(r), default=str)) for r in records]
        if key is not None:
            rows = last_per_key(rows, key)
        gone = deleted_keys(deleted_by)
        kept = [json.loads(text) for text in self._tables.get(table, [])]
        if key is not None:
            replaced = {json.dumps(row[key], sort_keys=True) for row in rows}
            kept = [
                r
                for r in kept
                if json.dumps(r.get(key), sort_keys=True) not in replaced
            ]
        kept += rows
        if gone:
            removed = {json.dumps(k, sort_keys=True) for k in gone}
            kept = [
                r for r in kept if json.dumps(r.get(key), sort_keys=True) not in removed
            ]
        if kept or table in self._tables:
            self._tables[table] = [json.dumps(r) for r in kept]
        self.writes += 1
        return len(rows)

    def read(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """`table`'s rows as dicts, at most `limit`, each with every column."""
        rows = [json.loads(text) for text in self._tables.get(table, [])]
        names = list(dict.fromkeys(n for row in rows for n in row))
        full = [{n: row.get(n) for n in names} for row in rows]
        return full if limit is None else full[:limit]

    def get(self, name: str) -> Any:
        """The state last saved under `name`, or None."""
        return self._state.get(name)

    def put(self, name: str, value: Any) -> None:
        """Save `value` under `name`, replacing what was there."""
        self._state.put(name, value)


class _Finished:
    """A tracker stand-in whose run is complete, listing `deleted`."""

    def __init__(self, deleted: list[Any] | None) -> None:
        self.deleted = deleted


def check_warehouse(make: Callable[[], Warehouse]) -> None:
    """Check a warehouse keeps the promises every Warehouse must keep.

    `make()` gives a new, empty warehouse each time it is called (a fresh
    file, say). Raises AssertionError naming the first promise broken. The
    promises are in api_explorer.warehouse; values may come back as
    written or as their JSON text.
    """

    def matches(rows: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
        def fits(row: dict[str, Any], want: dict[str, Any]) -> bool:
            return all(same_value(row.get(k), v) for k, v in want.items()) and all(
                row[k] is None for k in row if k not in want
            )

        left = list(rows)
        for want in expected:
            found = next((r for r in left if fits(r, want)), None)
            if found is None:
                return False
            left.remove(found)
        return not left

    def fail(promise: str, rows: list[dict[str, Any]]) -> AssertionError:
        return AssertionError(f"{promise}; the table holds {rows!r}")

    class SourceFailed(Exception):
        """The failure check_warehouse's source raises part way."""

    def failing(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        yield from rows
        raise SourceFailed

    varied = {
        "id": 1,
        "n": 2,
        "x": 2.5,
        "ok": True,
        "name": "Ann",
        "meta": {"a": [1, "b"]},
        "tags": ["new"],
        "none": None,
    }

    shop = make()
    if shop.read("orders") != []:
        raise fail("a table never written must read as []", shop.read("orders"))
    stored = shop.write("orders", [varied, {**varied, "id": 2}])
    if stored != 2:
        raise AssertionError(f"write must return how many it stored: 2, got {stored!r}")
    rows = shop.read("orders")
    if not matches(rows, [varied, {**varied, "id": 2}]):
        raise fail("read must give back what was written", rows)
    shop.write("orders", [{"id": 1, "name": "Ann"}])
    if len(shop.read("orders")) != 3:
        raise fail("without a key, write must add records", shop.read("orders"))

    shop = make()
    shop.write("orders", [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], key="id")
    shop.write("orders", [{"id": 1, "v": "old"}, {"id": 1, "v": "new"}], key="id")
    rows = shop.read("orders")
    if not matches(rows, [{"id": 1, "v": "new"}, {"id": 2, "v": "b"}]):
        raise fail("with a key, a record must replace its row, the last winning", rows)

    shop = make()
    try:
        shop.write("orders", failing([{"id": 1}, {"id": 2}]))
    except SourceFailed:
        pass
    else:
        raise AssertionError("write hid a failure of its records")
    if shop.read("orders") != []:
        raise fail(
            "a write whose records failed must store nothing", shop.read("orders")
        )
    shop.write("orders", [{"id": 1, "v": "a"}], key="id")
    try:
        shop.write(
            "orders", failing([{"id": 1, "v": "b"}, {"id": 2, "v": 1.5}]), key="id"
        )
    except SourceFailed:
        pass
    if not matches(shop.read("orders"), [{"id": 1, "v": "a"}]):
        raise fail(
            "a write whose records failed must leave the table as it was",
            shop.read("orders"),
        )
    refused = False
    try:
        shop.write("orders", [{"id": 3}, {"v": "no key"}], key="id")
    except Exception:  # noqa: BLE001 - any refusal will do
        refused = True
    if not refused:
        raise AssertionError("a record without its key must be refused")
    if not matches(shop.read("orders"), [{"id": 1, "v": "a"}]):
        raise fail("a refused write must store nothing", shop.read("orders"))

    shop = make()
    shop.write("orders", [{"id": 1}, {"id": 2}, {"id": 3}], key="id")
    shop.write("orders", [{"id": 4}], key="id", deleted_by=_Finished([1, 3]))
    if not matches(shop.read("orders"), [{"id": 2}, {"id": 4}]):
        raise fail(
            "deleted_by's keys must be removed once the records are stored",
            shop.read("orders"),
        )
    refused = False
    try:
        shop.write("orders", [{"id": 5}], key="id", deleted_by=_Finished(None))
    except Exception:  # noqa: BLE001 - any refusal will do
        refused = True
    if not refused:
        raise AssertionError("a deleted_by whose run isn't complete must be refused")
    if not matches(shop.read("orders"), [{"id": 2}, {"id": 4}]):
        raise fail("a refused write must store nothing", shop.read("orders"))

    shop = make()
    shop.write("t", [{"id": 1, "v": 1}, {"id": 2, "v": True}, {"id": 3, "v": {"a": 1}}])
    shop.write("t", [{"id": 4, "v": "text"}, {"id": 5, "v": 2.5}])
    expected = [
        {"id": i, "v": v}
        for i, v in [(1, 1), (2, True), (3, {"a": 1}), (4, "text"), (5, 2.5)]
    ]
    if not matches(shop.read("t"), expected):
        raise fail(
            "a change of kind must keep every value, as written or as JSON text",
            shop.read("t"),
        )

    shop = make()
    odd = {"id": 1, 'it\'s "odd"': "a", "drop table x; --": "b"}
    shop.write('my "table"; --', [odd])
    if not matches(shop.read('my "table"; --'), [odd]):
        raise fail(
            "table and field names must be stored as given", shop.read('my "table"; --')
        )
