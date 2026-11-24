"""An HTTP client for one API: URLs, retries, JSON, and paginated records."""

import gzip
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal, NamedTuple, Protocol, Self, runtime_checkable

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
    keeps_credentials,
)
from .incremental import Tracker
from .limits import RateLimit
from .logs import client_context, log_event
from .models import converter
from .paging import (
    RESULTS,
    CountMismatch,
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
    `trailing_slash=True`, a relative endpoint gets a "/" before any query,
    as Django REST Framework expects; by default it is sent as written.

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

    `off_host` says what happens to a request for another host than
    `base_url`'s (another host name or port, or a step down from https to
    http), such as an absolute next link that leads elsewhere. "refuse"
    (the default) raises ApiError before anything is sent: neither the
    client's credentials go there, nor that host's records come back into
    this API's results. "unsigned" sends it with the call's own headers
    only, without the client's headers and auth, and a "withheld" warning
    says so, for an API that really pages across hosts. A client with no
    `base_url` has no home host: it goes anywhere, and sends its headers
    and auth everywhere.

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

    `sleep` waits between retries: time.sleep unless given. Tests pass a
    function that records the waits instead (FakeApi.client() does), so
    they can check what the retry policy asked for without waiting.

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
        trailing_slash: bool = False,
        auth: Auth | None = None,
        transport: Transport | None = None,
        name: str | None = None,
        check: ResponseCheck | None = None,
        rate_limit: RateLimit | None = None,
        off_host: Literal["refuse", "unsigned"] = "refuse",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if off_host not in ("refuse", "unsigned"):
            raise ValueError(
                f"off_host must be 'refuse' or 'unsigned', got {off_host!r}"
            )
        self.off_host = off_host
        self.sleep = sleep
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
        with client_context(self.name), self._stamping(method.upper()):
            return self._send(method, endpoint, retry, kwargs)

    def _send(
        self, method: str, endpoint: str, retry: bool | None, kwargs: dict[str, Any]
    ) -> Response:
        method = method.upper()
        url = _with_params(self.url(endpoint), kwargs.pop("params", None))
        own = kwargs.pop("headers", None) or {}
        try:
            at_home = not self.base_url or keeps_credentials(self.base_url, url)
        except ValueError as exc:
            # A next link is the server's own text, and may not be a URL.
            raise ApiError(
                f"{method} {url} isn't a URL this client can read: {exc}", url=url
            ) from exc
        if at_home:
            # Merged by name without regard to case, the call's own winning.
            headers = Headers(self.headers)
            headers.update(own)
        elif self.off_host == "refuse":
            # Before anything is sent: what that host answered would pass
            # for this API's own data.
            raise ApiError(
                f"{method} {url} is on another host than {self.base_url}, and "
                "this client refuses to leave its API's host; if the API "
                "really pages across hosts, pass Client(off_host='unsigned') "
                "to send it without this client's headers and auth",
                url=url,
            )
        else:
            # The client's headers and auth belong to its API's host; a URL
            # elsewhere, such as a next link, must not carry them.
            headers = Headers(own)
            log_event(
                logger,
                logging.WARNING,
                "withheld",
                "%s %s is on another host than %s, so it goes without this "
                "client's headers and auth",
                method,
                url,
                self.base_url,
                data={"method": method, "url": url, "base_url": self.base_url},
            )
        request = Request(method, url, headers=headers.raw(), **kwargs)
        attempt = 0
        while True:
            attempt += 1
            response: Response | None = None
            error: Exception | None = None
            problem: Problem | None = None
            try:
                response = self._exchange(request, self.auth if at_home else None)
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
            self.sleep(wait)

    def _exchange(self, request: Request, auth: Auth | None) -> Response:
        """Send `request` through `auth`, if any, and the transport.

        An auth's flow may send requests of its own first (to renew a token)
        and send the request again after a 401; every one goes through the
        transport, so a rate limit counts them all.
        """
        if auth is None:
            return self._one(request)
        flow = auth.auth_flow(request)
        # Closed whatever happens: a flow paused mid-renewal holds its lock
        # (TokenRefresh does), and a send that raises would leave it held,
        # so the retry after it would wait on that lock for ever.
        try:
            try:
                outgoing = next(flow)
            except StopIteration:
                raise TypeError(
                    f"{type(auth).__name__}.auth_flow yielded no request to send"
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

    @contextmanager
    def _stamping(self, method: str) -> Iterator[None]:
        """Give an ApiError leaving the block the `method` and this client's name.

        Only where it has none: an auth names its own token request, and a
        piece of your own may name what it likes.
        """
        try:
            yield
        except ApiError as error:
            if error.method is None:
                error.method = method
            if error.client is None:
                error.client = self.name
            raise

    def _stamped(self, records: Iterator[Any], method: str) -> Iterator[Any]:
        """Yield from `records`, stamping an ApiError they raise (see _stamping)."""
        with self._stamping(method):
            yield from records

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
                method=response.request.method,
                client=self.name,
            )
        if content[:2] == b"\x1f\x8b":
            try:
                content = gzip.decompress(content)
            except (OSError, EOFError) as exc:
                raise ApiError(
                    f"{where} sent a gzip body that won't unpack: {exc}",
                    url=str(response.url),
                    status=response.status_code,
                    method=response.request.method,
                    client=self.name,
                ) from exc
        try:
            return self.decode(content)
        except ValueError as exc:
            raise ApiError(
                f"{where} did not return JSON ({kind}): {exc}; it began "
                f"{content[:120]!r}",
                url=str(response.url),
                status=response.status_code,
                method=response.request.method,
                client=self.name,
            ) from exc

    def records(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        results_key: str | None = RESULTS,
        count_key: str | None = None,
        paging: Paging | None = None,
        max_pages: int | None = None,
        model: Any = None,
        retry: bool | None = None,
        incremental: Tracker | None = None,
        strict_count: bool = False,
    ) -> Iterator[Any]:
        """Yield every record from a paginated JSON endpoint.

        `results_key` is the path to each page's list of records, with
        fallbacks such as "results|data.rows". The default, RESULTS, tries
        "results", "items" and "data", and takes a body that is itself a
        list; name one key to be strict, and pass None when the body is
        always the list. `count_key` is the optional path to the
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

        `strict_count` checks the run against `count_key`'s count, for a
        run that reads a whole listing from its start: the count must be
        the same on every page, and the records received must equal it at
        the end, or CountMismatch (an ApiError) is raised. Paging by page
        number or offset skips or repeats a record when the data changes
        during a run, and this is how to notice: catch it and read again.
        It is raised after the last record is delivered, so store nothing
        for good until records() ends (a warehouse write does this). A
        page without a count is an ApiError. With `max_pages` only the
        pages are compared, since the run stops short of the count.

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
        if strict_count and count_key is None:
            raise ValueError(
                "strict_count needs a count_key, the count to check against"
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
        with self._stamping(start.method):
            if incremental is not None:
                start = incremental.apply(start)
            first, next_link = (paging or NextLink()).begin(start, read)
        step = follow(next_link)

        def next_request(timed: _Timed, request: PageRequest) -> PageRequest | None:
            with client_context(self.name):
                return step(timed.fetched, request)

        def every_record() -> Iterator[Any]:
            received = pages = 0
            count: int | None = None  # the first page's, for strict_count
            started = time.monotonic()
            for pages, timed in enumerate(
                paginate(fetch, first, next_request, max_pages=max_pages), start=1
            ):
                with client_context(self.name):
                    items, total = read(timed.fetched)
                    received += len(items)
                    _log_page(pages, len(items), received, total, timed.seconds)
                    if strict_count:
                        where = str(timed.fetched.response.url)
                        count = _same_count(count, total, received, count_key, where)
                yield from items
            with client_context(self.name):
                _log_done(pages, received, time.monotonic() - started, max_pages)
            if count is not None and max_pages is None and received != count:
                raise CountMismatch(
                    f"The pages held {received:,} records, but the count at "
                    f"{count_key!r} is {count:,}; the data probably changed "
                    "during the run, so read again",
                    count=count,
                    records=received,
                    url=url,
                )

        stream = every_record()
        if incremental is not None:
            stream = self._in_context(incremental.track(stream))
        if convert is None:
            return self._stamped(stream, start.method)

        def converted() -> Iterator[Any]:
            for index, item in enumerate(stream):
                try:
                    yield convert(item)
                except (ValueError, TypeError) as exc:
                    raise ApiError(
                        f"Record {index} doesn't fit {name}: {exc}", url=url
                    ) from exc

        return self._stamped(converted(), start.method)

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


def _same_count(
    count: int | None,
    total: int | None,
    received: int,
    count_key: str | None,
    url: str,
) -> int:
    """strict_count's check of one page: its count, which must be the run's.

    `count` is the first page's count (None on the first page), `total`
    this page's, and `received` the records read so far.
    """
    if total is None:
        raise ApiError(
            f"strict_count needs a count on every page, and there is none at "
            f"{count_key!r}",
            url=url,
        )
    if count is not None and total != count:
        raise CountMismatch(
            f"The count at {count_key!r} changed from {count:,} to {total:,} "
            "between pages; the data changed during the run, so read again",
            count=count,
            records=received,
            url=url,
        )
    return total


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
