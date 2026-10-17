"""An HTTP client for one API: URLs, retries, JSON, and paginated records."""

import dataclasses
import gzip
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol, Self, runtime_checkable
from urllib.parse import urljoin, urlsplit

import httpx2

from .checks import ResponseCheck
from .decode import Decoder, json_decoder
from .errors import ApiError, Problem
from .logs import client_context, log_event
from .models import converter
from .paging import (
    Fetched,
    NextLink,
    Page,
    PageRequest,
    Paging,
    paginate,
    read_page,
)
from .retry import TEMPORARY_STATUSES, Failure, Retry, RetryPolicy

logger = logging.getLogger(__name__)


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

    `timeout` is seconds for every stage of a request (20 by default), or
    an httpx2.Timeout for separate limits, such as
    httpx2.Timeout(60, connect=5): give up quickly when the server is
    down, but wait for a slow page.

    `retry` decides when a failed request is tried again: any RetryPolicy.
    The default, Retry(), gives up to 3 tries for network errors, timeouts,
    rate limits and temporary server errors, for idempotent methods only.
    `decode` parses JSON bodies (see json_decoder); by default msgspec when
    installed.

    `auth` signs every request, for tokens that change, such as
    TokenRefresh. Its own URLs (login, refresh) follow the endpoint rules.
    A fixed token can go in `headers` instead (token_headers).

    `http` is an httpx2.Client of your own, for anything httpx2 can do that
    this class doesn't ask about: proxies, HTTP/2, certificates, event
    hooks. Its base URL, headers, timeout, auth, transport and redirect
    setting apply as they are, so passing any of those here as well is
    refused: each setting has one place. A client you bring stays open
    when this one closes; it is yours to close.

    `check` looks at every successful response and says whether it really
    failed, for APIs that answer 200 with an error in the body (see
    error_at). A problem it finds goes to the retry policy like any other
    failure: Retry retries a temporary one and stops on a permanent one, so
    a run never carries on past a page that reported an error.

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
        timeout: float | httpx2.Timeout | None = None,
        retry: RetryPolicy | None = None,
        decode: Decoder | None = None,
        trailing_slash: bool = True,
        auth: httpx2.Auth | None = None,
        transport: httpx2.BaseTransport | None = None,
        http: httpx2.Client | None = None,
        name: str | None = None,
        check: ResponseCheck | None = None,
    ) -> None:
        self.retry: RetryPolicy = retry or Retry()
        self.decode = decode or json_decoder()
        self.trailing_slash = trailing_slash
        self.name = name
        self.check = check
        if http is None:
            self._http = httpx2.Client(
                base_url=base_url,
                headers=dict(headers or {}),
                timeout=20.0 if timeout is None else timeout,
                follow_redirects=True,
                auth=auth,
                transport=transport,
            )
            self._owns_http = True
        else:
            settings = {
                "base_url": base_url,
                "headers": headers,
                "timeout": timeout,
                "auth": auth,
                "transport": transport,
            }
            doubled = [name for name, value in settings.items() if value]
            if doubled:
                raise ValueError(
                    f"Set {', '.join(doubled)} on the httpx2.Client you pass as http, "
                    "not here as well"
                )
            self._http = http
            self._owns_http = False
        # An auth with URLs of its own (TokenRefresh) resolves them the way
        # endpoints are resolved, so one rule covers every URL.
        if isinstance(self._http.auth, ResolvesUrls):
            self._http.auth.bind(self.url)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connections, unless the httpx2 client was passed in as `http`."""
        if self._owns_http:
            self._http.close()

    def url(self, endpoint: str) -> str:
        """Return the absolute URL that a request to `endpoint` goes to."""
        if urlsplit(endpoint).scheme:
            return endpoint
        if self.trailing_slash:
            path, mark, query = endpoint.partition("?")
            if not path.endswith("/"):
                endpoint = f"{path}/{mark}{query}"
        return str(self._http.base_url.join(endpoint.lstrip("/")))

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        retry: bool | None = None,
        **kwargs: Any,
    ) -> httpx2.Response:
        """Send a request and return the response, or raise ApiError.

        A failure goes to the retry policy, which says whether to try again
        and how long to wait first; each retry logs a warning. `retry`
        overrides it for this call: False never retries, True retries
        whatever the method. Other keyword arguments go to httpx2: params=,
        json=, data=, headers= and so on.
        """
        with client_context(self.name):
            return self._send(method, endpoint, retry, kwargs)

    def _send(
        self, method: str, endpoint: str, retry: bool | None, kwargs: dict[str, Any]
    ) -> httpx2.Response:
        method = method.upper()
        url = self.url(endpoint)
        params = kwargs.pop("params", None)
        if params:
            # Merge into any query the endpoint already has; httpx2 alone
            # would replace it (the httpx 0.28 behaviour, httpx #3433).
            url = str(httpx2.URL(url).copy_merge_params(params))
        attempt = 0
        while True:
            attempt += 1
            response: httpx2.Response | None = None
            error: Exception | None = None
            problem: Problem | None = None
            try:
                response = self._http.request(method, url, **kwargs)
            # A dropped connection mid-response (RemoteProtocolError) is as
            # passing as a timeout, so it goes to the retry policy too.
            except (
                httpx2.TimeoutException,
                httpx2.NetworkError,
                httpx2.RemoteProtocolError,
            ) as exc:
                error = exc
                failure = ApiError(f"{method} {url} failed: {exc}", url=url)
                failure.__cause__ = exc
            except httpx2.HTTPError as exc:
                raise ApiError(f"{method} {url} failed: {exc}", url=url) from exc
            except ApiError as exc:
                # Raised by an auth while renewing its token. A temporary
                # failure at the token endpoint goes to the retry policy,
                # like any other; anything else is final.
                if exc.status not in TEMPORARY_STATUSES:
                    raise
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

    def _problem(self, response: httpx2.Response) -> Problem | None:
        if self.check is None:
            return None
        verdict = self.check(response)
        return Problem(verdict) if isinstance(verdict, str) else verdict

    def get_json(self, endpoint: str, **kwargs: Any) -> Any:
        """GET `endpoint` and return its parsed JSON body, or raise ApiError."""
        return self.json_body(self.request("GET", endpoint, **kwargs))

    def json_body(self, response: httpx2.Response) -> Any:
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
            content = gzip.decompress(content)
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
    ) -> Iterator[Any]:
        """Yield every record from a paginated JSON endpoint.

        `results_key` is the path to each page's list of records, with
        fallbacks such as "results|data|items". results_key=None means the
        body itself is the list. `count_key` is the optional path to the
        total record count, such as "count". It shows in the progress log,
        and lets Offset stop exactly at the end. `paging` says how to reach
        the next page: NextLink() (the default) follows a next-page URL in
        each page, Offset(...) steps a limit/offset query, HeaderLink()
        follows the Link header, and Cursor(...) sends back a cursor.
        `params` are added to the first URL; later pages carry or work out
        their own.

        `method` and `json` make the first request, such as a POST search
        with its query in the body; later pages keep them unless the paging
        style changes them (Cursor(send_in="body") does). `retry` is passed
        to each request: a POST isn't retried by default, since it might
        change data, so pass retry=True for a search that only reads.
        `model` turns each record into a typed object (see converter): a
        pydantic model, msgspec Struct, dataclass or function.

        Each page logs a "page" event at INFO, such as "Page 44: 100 records
        in 0.62s, 4,400 so far of 61,203", and the end a "done" event, so a
        long run can be watched; record.data carries the numbers. Retries log
        as warnings. Nothing shows until logging is switched on (see
        enable_logging), and every line is stamped with this client's name.

        records() is built from public parts, so a loop of your own can
        reuse them: paginate() follows the pages, a Paging finds the next
        one, read_page() finds the records and the count, and converter()
        applies the model.

        Raises ApiError when a request fails, when the results path is
        missing or isn't a list, when a record doesn't fit `model`, or when
        paging finds something wrong. A missing results path is an error,
        not an empty page, so a mistyped key can't pass for "no data". An
        explicit null is an empty page.
        """
        url = httpx2.URL(self.url(endpoint)).copy_merge_params(params or {})
        convert = None if model is None else converter(model)
        name = getattr(model, "__name__", repr(model))
        warned_no_count = False

        def read(fetched: Fetched) -> Page:
            nonlocal warned_no_count
            result = read_page(
                fetched.body, results_key, count_key, url=str(fetched.response.url)
            )
            if count_key is not None and result.total is None and not warned_no_count:
                warned_no_count = True
                log_event(
                    logger,
                    logging.WARNING,
                    "no-count",
                    "No count at %r in the response",
                    count_key,
                    data={"count_key": count_key},
                )
            return result

        seconds = 0.0

        def fetch(request: PageRequest) -> Fetched:
            nonlocal seconds
            started = time.monotonic()
            response = self.request(
                request.method, request.url, json=request.json, retry=retry
            )
            fetched = Fetched(self.json_body(response), response)
            seconds = time.monotonic() - started
            return fetched

        start = PageRequest(str(url), method.upper(), json)
        first, next_link = (paging or NextLink()).begin(start, read)

        def next_request(fetched: Fetched, request: PageRequest) -> PageRequest | None:
            # A URL from the style keeps the method and body (see Paging).
            with client_context(self.name):
                following = next_link(fetched, request)
            if isinstance(following, str):
                if not following:
                    return None
                return dataclasses.replace(request, url=urljoin(request.url, following))
            return following

        received = 0
        pages = 0
        started = time.monotonic()
        for number, fetched in enumerate(
            paginate(fetch, first, next_request, max_pages=max_pages), start=1
        ):
            pages = number
            with client_context(self.name):
                items, total = read(fetched)
                received += len(items)
                of_total = "" if total is None else f" of {total:,}"
                log_event(
                    logger,
                    logging.INFO,
                    "page",
                    "Page %d: %d records in %.2fs, %s so far%s",
                    number,
                    len(items),
                    seconds,
                    f"{received:,}",
                    of_total,
                    data={
                        "page": number,
                        "records_on_page": len(items),
                        "records_so_far": received,
                        "total": total,
                        "seconds": seconds,
                    },
                )
            if convert is None:
                yield from items
                continue
            for index, item in enumerate(items, start=received - len(items)):
                try:
                    yield convert(item)
                except (ValueError, TypeError) as exc:
                    raise ApiError(
                        f"Record {index} doesn't fit {name}: {exc}", url=str(url)
                    ) from exc
        with client_context(self.name):
            if max_pages is not None and pages >= max_pages:
                log_event(
                    logger,
                    logging.DEBUG,
                    "stop",
                    "Stopped at max_pages=%d",
                    max_pages,
                    data={"reason": "max_pages"},
                )
            elapsed = time.monotonic() - started
            log_event(
                logger,
                logging.INFO,
                "done",
                "Done: %s records in %d pages, %.1fs",
                f"{received:,}",
                pages,
                elapsed,
                data={"pages": pages, "records": received, "seconds": elapsed},
            )
