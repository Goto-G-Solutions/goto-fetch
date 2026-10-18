"""An HTTP client for one API: URLs, retries, JSON, and paginated records."""

import gzip
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, NamedTuple, Protocol, Self, runtime_checkable

from .checks import ResponseCheck, as_problem
from .decode import Decoder, json_decoder
from .errors import ApiError, Problem
from .http import (
    URL,
    Auth,
    Headers,
    NetworkError,
    Request,
    Response,
    Timeout,
    Transport,
    TransportError,
    is_absolute,
    join_url,
)
from .incremental import Tracker
from .limits import RateLimit
from .logs import client_context, log_event
from .models import converter
from .paging import (
    Fetched,
    NextLink,
    Page,
    PageRequest,
    Paging,
    follow,
    paginate,
    read_page,
)
from .retry import Failure, Retry, RetryPolicy
from .urllib3_transport import Urllib3Transport

logger = logging.getLogger(__name__)
http_logger = logging.getLogger("goto_fetch.http")
"""Where each request sent logs its "request" event (enable_logging(http=True))."""


@runtime_checkable
class ResolvesUrls(Protocol):
    """An auth with URLs of its own, such as TokenRefresh's login and refresh URLs.

    Client calls bind(resolve) with its own url(), so those URLs follow the
    same rules as endpoints: relative to the base URL, trailing slash
    included. A custom auth opts in by having this method.
    """

    def bind(self, resolve: Callable[[str], str]) -> None: ...


class Client:
    """An HTTP client for one API.

    Relative endpoints join onto `base_url`: "/items/" under
    "https://host/api" is "https://host/api/items/". Absolute URLs, such as
    the next-page links APIs return, are used as given. With
    `trailing_slash`, a relative endpoint gets a "/" before any query, as
    Django REST Framework expects.

    `timeout` is seconds to wait at each stage of a request, connecting
    and each read (20 by default), or a Timeout for separate limits, such
    as Timeout(connect=5, read=60): give up quickly when a server is down,
    but wait for a slow page.

    `retry` decides when a failed request is tried again: any RetryPolicy.
    The default, Retry(), gives up to 3 tries for network errors, timeouts,
    rate limits and temporary server errors, for idempotent methods only.
    `decode` parses JSON bodies (see json_decoder); by default msgspec when
    installed.

    `auth` signs every request, for tokens that change, such as
    TokenRefresh: any Auth (see goto_fetch.http). Its own URLs (login,
    refresh) follow the endpoint rules. A fixed token can go in `headers`
    instead (token_headers).

    `transport` sends the requests: Urllib3Transport by default, or any
    Transport, such as Httpx2Transport for HTTP/2 or an httpx2.Client of
    your own. Every request goes through it, token requests and retries
    included. A transport you pass stays open when this client closes, so
    several clients can share one;
    it is yours to close.

    `check` looks at every successful response and says whether it really
    failed, for APIs that answer 200 with an error in the body (see
    error_at). A problem it finds goes to the retry policy like any other
    failure: Retry retries a temporary one and stops on a permanent one, so
    a run never carries on past a page that reported an error.

    `rate_limit` keeps every request this client sends (pages, retries and
    token requests alike) to a RateLimit, for servers that can't take a
    fast run; pass one RateLimit to several clients for one budget.

    `name` labels this client's log lines (record.client), including those
    from pieces you build that log during its requests: useful when one
    pipeline talks to several APIs. See enable_logging.

    Use it in a `with` block, or call close() when done.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | Timeout | None = 20.0,
        retry: RetryPolicy | None = None,
        decode: Decoder | None = None,
        trailing_slash: bool = True,
        auth: Auth | None = None,
        transport: Transport | None = None,
        name: str | None = None,
        check: ResponseCheck | None = None,
        rate_limit: RateLimit | None = None,
    ) -> None:
        self.base_url = base_url
        self.headers = dict(headers or {})
        self.timeout = Timeout.of(timeout)
        self.retry: RetryPolicy = retry or Retry()
        self.decode = decode or json_decoder()
        self.trailing_slash = trailing_slash
        self.auth = auth
        self.name = name
        self.check = check
        self.rate_limit = rate_limit
        self._owns_transport = transport is None
        self.transport: Transport = transport or Urllib3Transport()
        # An auth with URLs of its own (TokenRefresh) resolves them the way
        # endpoints are resolved, so one rule covers every URL.
        if isinstance(auth, ResolvesUrls):
            auth.bind(self.url)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connections, unless the transport was passed in."""
        if self._owns_transport:
            self.transport.close()

    def url(self, endpoint: str) -> str:
        """Return the absolute URL that a request to `endpoint` goes to."""
        if is_absolute(endpoint):
            return endpoint
        if self.trailing_slash:
            # The slash goes on the path, before any query or fragment.
            cut = min(
                (i for i in (endpoint.find("?"), endpoint.find("#")) if i >= 0),
                default=len(endpoint),
            )
            path, rest = endpoint[:cut], endpoint[cut:]
            if not path.endswith("/"):
                endpoint = f"{path}/{rest}"
        return join_url(self.base_url, endpoint)

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        retry: bool | None = None,
        **kwargs: Any,
    ) -> Response:
        """Send a request and return the response, or raise ApiError.

        A failure goes to the retry policy, which says whether to try again
        and how long to wait first; each retry logs a warning. `retry`
        overrides it for this call: False never retries, True retries
        whatever the method. Other keyword arguments build the Request:
        params=, headers=, json=, data= (a form, or bytes) or content=.
        """
        with client_context(self.name):
            return self._send(method, endpoint, retry, kwargs)

    def _send(
        self, method: str, endpoint: str, retry: bool | None, kwargs: dict[str, Any]
    ) -> Response:
        method = method.upper()
        url = _with_params(self.url(endpoint), kwargs.pop("params", None))
        # Merged by name without regard to case, the call's own winning.
        headers = Headers(self.headers)
        headers.update(kwargs.pop("headers", None) or {})
        request = Request(method, url, headers=headers.raw(), **kwargs)
        attempt = 0
        while True:
            attempt += 1
            response: Response | None = None
            error: Exception | None = None
            problem: Problem | None = None
            try:
                response = self._exchange(request)
            # No connection, a timeout, or a reply cut short: each may pass,
            # so it goes to the retry policy.
            except NetworkError as exc:
                error = exc
                failure = ApiError(f"{method} {url} failed: {exc}", url=url)
                failure.__cause__ = exc
            except TransportError as exc:
                raise ApiError(f"{method} {url} failed: {exc}", url=url) from exc
            except ApiError as exc:
                # Raised by an auth while renewing its token. The retry
                # policy decides, by the token endpoint's status, as for
                # any other failure (see Failure).
                error = failure = exc
            else:
                if response.is_success:
                    problem = self._problem(response)
                    if problem is None:
                        return response
                    failure = ApiError(
                        f"{method} {response.url} returned HTTP "
                        f"{response.status_code} but reports a problem: "
                        f"{problem.message}",
                        url=str(response.url),
                        status=response.status_code,
                    )
                else:
                    failure = ApiError(
                        f"{method} {response.url} returned HTTP {response.status_code}: "
                        f"{response.text[:300]}",
                        url=str(response.url),
                        status=response.status_code,
                    )
            if retry is False:
                raise failure
            wait = self.retry.wait(
                Failure(attempt, method, url, response, error, retry, problem)
            )
            status = None if response is None else response.status_code
            details = {
                "method": method,
                "url": url,
                "attempt": attempt,
                "status": status,
            }
            if wait is None:
                log_event(
                    logger,
                    logging.DEBUG,
                    "give-up",
                    "The retry policy gave up after try %d of %s %s",
                    attempt,
                    method,
                    url,
                    data=details,
                )
                raise failure
            log_event(
                logger,
                logging.WARNING,
                "retry",
                "%s; retry %d in %.1fs",
                failure,
                attempt,
                wait,
                data={**details, "wait": wait},
            )
            time.sleep(wait)

    def _exchange(self, request: Request) -> Response:
        """Send `request` through the auth, if any, and the transport.

        An auth's flow may send requests of its own first (to renew a token)
        and send the request again after a 401; every one goes through the
        transport, so a rate limit counts them all.
        """
        if self.auth is None:
            return self._one(request)
        flow = self.auth.auth_flow(request)
        # Closed whatever happens: a flow paused mid-renewal holds its lock
        # (TokenRefresh does), and a send that raises would leave it held,
        # so the retry after it would wait on that lock for ever.
        try:
            try:
                outgoing = next(flow)
            except StopIteration:
                raise TypeError(
                    f"{type(self.auth).__name__}.auth_flow yielded no request to send"
                ) from None
            while True:
                response = self._one(outgoing)
                try:
                    outgoing = flow.send(response)
                except StopIteration:
                    return response
        finally:
            flow.close()

    def _one(self, request: Request) -> Response:
        """Send one request through the transport, logging a "request" event.

        A send that raises logs one too, with no status and the error, so
        every request sent shows in the log.
        """
        if self.rate_limit is not None:
            self.rate_limit.wait()
        started = time.monotonic()
        status: int | None = None
        error: str | None = None
        try:
            response = self.transport.send(request, self.timeout)
            status = response.status_code
            return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            seconds = time.monotonic() - started
            log_event(
                http_logger,
                logging.INFO,
                "request",
                "%s %s: %s in %.2fs",
                request.method,
                request.url,
                f"HTTP {status}" if status is not None else error,
                seconds,
                data={
                    "method": request.method,
                    "url": str(request.url),
                    "status": status,
                    "seconds": seconds,
                    "error": error,
                },
            )

    def _in_context(self, records: Iterator[Any]) -> Iterator[Any]:
        """Yield from `records`, stamping their log lines with this client's name.

        The name is set only while the next record is being found, not while
        the caller holds it, so the caller's own log lines aren't stamped.
        """
        while True:
            with client_context(self.name):
                try:
                    record = next(records)
                except StopIteration:
                    return
            yield record

    def _problem(self, response: Response) -> Problem | None:
        if self.check is None:
            return None
        return as_problem(self.check(response))

    def get_json(self, endpoint: str, **kwargs: Any) -> Any:
        """GET `endpoint` and return its parsed JSON body, or raise ApiError."""
        return self.json_body(self.request("GET", endpoint, **kwargs))

    def json_body(self, response: Response) -> Any:
        """Parse a response's JSON body with this client's decoder, or raise ApiError.

        A 204 or 205, which has no body by definition, gives None. Any other
        empty body is an error, not an empty page: an API answering 200 with
        nothing (often a proxy or gateway fault) must not pass for "no
        data". A gzip body sent without a Content-Encoding header is
        recognised by its first bytes and unpacked.
        """
        content = response.content
        where = f"{response.request.method} {response.url}"
        kind = response.headers.get("Content-Type", "no Content-Type")
        if not content.strip():
            if response.status_code in (204, 205):
                return None
            raise ApiError(
                f"{where} returned HTTP {response.status_code} with an empty body ({kind})",
                url=str(response.url),
                status=response.status_code,
            )
        if content[:2] == b"\x1f\x8b":
            try:
                content = gzip.decompress(content)
            except (OSError, EOFError) as exc:
                raise ApiError(
                    f"{where} sent a gzip body that won't unpack: {exc}",
                    url=str(response.url),
                    status=response.status_code,
                ) from exc
        try:
            return self.decode(content)
        except ValueError as exc:
            raise ApiError(
                f"{where} did not return JSON ({kind}): {exc}; it began "
                f"{content[:120]!r}",
                url=str(response.url),
                status=response.status_code,
            ) from exc

    def records(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        results_key: str | None = "results",
        count_key: str | None = None,
        paging: Paging | None = None,
        max_pages: int | None = None,
        model: Any = None,
        retry: bool | None = None,
        incremental: Tracker | None = None,
    ) -> Iterator[Any]:
        """Yield every record from a paginated JSON endpoint.

        `results_key` is the path to each page's list of records, with
        fallbacks such as "results|data|items". results_key=None means the
        body itself is the list. `count_key` is the optional path to the
        total record count, such as "count". It goes in each "page" event,
        and lets Offset stop exactly at the end. `paging` says how to reach
        the next page: NextLink() (the default) follows a next-page URL in
        each page, Offset(...) steps a limit/offset query, HeaderLink()
        follows the Link header, Cursor(...) sends back a cursor, and
        Keyset(...) asks each page from the last value of the one before.
        `params` are added to the first URL; later pages carry or work out
        their own. `max_pages` stops after that many pages, for a look at
        the first few.

        `method` and `json` make the first request, such as a POST search
        with its query in the body; later pages keep them unless the paging
        style changes them (Cursor(send_in="body") does). `retry` is passed
        to each request: a POST isn't retried by default, since it might
        change data, so pass retry=True for a search that only reads.
        `model` turns each record into a typed object (see converter): a
        pydantic model, msgspec Struct, dataclass or function.

        `incremental` delivers only what changed since the last run: an
        Incremental asks the API from its last value on, and Changes
        fetches everything and passes on new and changed records. Save
        its state after storing the records (tracked_run does both); it
        moves on only when every page has been read, so it can't be
        used with `max_pages`.

        Each page logs a "page" event at INFO, such as "Page 44: 100 records
        in 0.62s, 4,400 so far of 61,203", and the end a "done" event, so a
        long run can be watched; record.data carries the numbers. Retries log
        as warnings. Nothing shows until logging is switched on (see
        enable_logging), and every line is stamped with this client's name.

        records() is built from public parts, so a loop of your own can
        reuse them: paginate() follows the pages, a Paging and follow() find
        the next one, read_page() finds the records and the count, and
        converter() applies the model.

        Raises ApiError when a request fails, when the results path is
        missing or isn't a list, when a record doesn't fit `model`, or when
        paging finds something wrong. A missing results path is an error,
        not an empty page, so a mistyped key can't pass for "no data". An
        explicit null is an empty page.
        """
        if incremental is not None and max_pages is not None:
            # A run cut short ends like a complete one, so the tracker would
            # move past the pages never fetched (or call their records
            # deleted).
            raise ValueError(
                "max_pages can't be used with incremental: a run cut short "
                "would skip the records it didn't fetch"
            )
        url = _with_params(self.url(endpoint), params)
        convert = None if model is None else converter(model)
        name = getattr(model, "__name__", repr(model))
        read = self._page_reader(results_key, count_key)

        def fetch(request: PageRequest) -> _Timed:
            started = time.monotonic()
            response = self.request(
                request.method, request.url, json=request.json, retry=retry
            )
            return _Timed(
                Fetched(self.json_body(response), response),
                time.monotonic() - started,
            )

        start = PageRequest(url, method.upper(), json)
        if incremental is not None:
            start = incremental.apply(start)
        first, next_link = (paging or NextLink()).begin(start, read)
        step = follow(next_link)

        def next_request(timed: _Timed, request: PageRequest) -> PageRequest | None:
            with client_context(self.name):
                return step(timed.fetched, request)

        def every_record() -> Iterator[Any]:
            received = pages = 0
            started = time.monotonic()
            for pages, timed in enumerate(
                paginate(fetch, first, next_request, max_pages=max_pages), start=1
            ):
                with client_context(self.name):
                    items, total = read(timed.fetched)
                    received += len(items)
                    _log_page(pages, len(items), received, total, timed.seconds)
                yield from items
            with client_context(self.name):
                _log_done(pages, received, time.monotonic() - started, max_pages)

        stream = every_record()
        if incremental is not None:
            stream = self._in_context(incremental.track(stream))
        if convert is None:
            yield from stream
            return
        for index, item in enumerate(stream):
            try:
                yield convert(item)
            except (ValueError, TypeError) as exc:
                raise ApiError(
                    f"Record {index} doesn't fit {name}: {exc}", url=url
                ) from exc

    def _page_reader(
        self, results_key: str | None, count_key: str | None
    ) -> Callable[[Fetched], Page]:
        """read(fetched) for one run: each page read once, a missing count warned once.

        The paging style and the loop both read each page; the last page
        read is kept, so the second read costs nothing.
        """
        last: tuple[Fetched, Page] | None = None
        warned = False

        def read(fetched: Fetched) -> Page:
            nonlocal last, warned
            if last is not None and last[0] is fetched:
                return last[1]
            page = read_page(
                fetched.body, results_key, count_key, url=str(fetched.response.url)
            )
            if count_key is not None and page.total is None and not warned:
                warned = True
                log_event(
                    logger,
                    logging.WARNING,
                    "no-count",
                    "No count at %r in the response",
                    count_key,
                    data={"count_key": count_key},
                )
            last = (fetched, page)
            return page

        return read


class _Timed(NamedTuple):
    """A fetched page, and how long fetching it took."""

    fetched: Fetched
    seconds: float


def _with_params(url: str, params: Mapping[str, Any] | None) -> str:
    """`url` with `params` merged into its query.

    Merged, not replaced, so the query the endpoint already has is kept.
    """
    if not params:
        return url
    return URL(url).copy_merge_params(params)


def _log_page(
    number: int, count: int, received: int, total: int | None, seconds: float
) -> None:
    """Log a "page" event: one line per page, for watching a long run."""
    of_total = "" if total is None else f" of {total:,}"
    log_event(
        logger,
        logging.INFO,
        "page",
        "Page %d: %d records in %.2fs, %s so far%s",
        number,
        count,
        seconds,
        f"{received:,}",
        of_total,
        data={
            "page": number,
            "records_on_page": count,
            "records_so_far": received,
            "total": total,
            "seconds": seconds,
        },
    )


def _log_done(pages: int, received: int, seconds: float, max_pages: int | None) -> None:
    """Log the "done" event a run ends with, after a "stop" event if max_pages cut it."""
    if max_pages is not None and pages >= max_pages:
        log_event(
            logger,
            logging.DEBUG,
            "stop",
            "Stopped at max_pages=%d",
            max_pages,
            data={"reason": "max_pages"},
        )
    log_event(
        logger,
        logging.INFO,
        "done",
        "Done: %s records in %d pages, %.1fs",
        f"{received:,}",
        pages,
        seconds,
        data={"pages": pages, "records": received, "seconds": seconds},
    )
