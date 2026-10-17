"""An HTTP client for one API: URLs, retries, JSON, and paginated records."""

import logging
import time
from collections.abc import Iterator, Mapping
from typing import Any, Self
from urllib.parse import urlsplit

import httpx2

from .decode import Decoder, json_decoder
from .errors import ApiError
from .models import converter
from .paging import NextLink, Page, Paging, Progress, paginate, read_page
from .retry import Failure, Retry, RetryPolicy

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.retry: RetryPolicy = retry or Retry()
        self.decode = decode or json_decoder()
        self.trailing_slash = trailing_slash
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
        bind = getattr(self._http.auth, "bind", None)
        if callable(bind):
            bind(self.url)

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
        method = method.upper()
        url = self.url(endpoint)
        attempt = 0
        while True:
            attempt += 1
            response: httpx2.Response | None = None
            error: Exception | None = None
            try:
                response = self._http.request(method, url, **kwargs)
            except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
                error = exc
                failure = ApiError(f"{method} {url} failed: {exc}", url=url)
                failure.__cause__ = exc
            except httpx2.HTTPError as exc:
                raise ApiError(f"{method} {url} failed: {exc}", url=url) from exc
            else:
                if response.is_success:
                    return response
                failure = ApiError(
                    f"{method} {response.url} returned HTTP {response.status_code}: "
                    f"{response.text[:300]}",
                    url=str(response.url),
                    status=response.status_code,
                )
            if retry is False:
                raise failure
            wait = self.retry.wait(
                Failure(attempt, method, url, response, error, retry)
            )
            if wait is None:
                raise failure
            logger.warning("%s; retry %d in %.1fs", failure, attempt, wait)
            time.sleep(wait)

    def get_json(self, endpoint: str, **kwargs: Any) -> Any:
        """GET `endpoint` and return its parsed JSON body, or raise ApiError."""
        response = self.request("GET", endpoint, **kwargs)
        try:
            return self.decode(response.content)
        except ValueError as exc:
            raise ApiError(
                f"GET {response.url} did not return JSON: {exc}",
                url=str(response.url),
                status=response.status_code,
            ) from exc

    def records(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        results_key: str | None = "results",
        count_key: str | None = None,
        paging: Paging | None = None,
        max_pages: int | None = None,
        model: Any = None,
    ) -> Iterator[Any]:
        """Yield every record from a paginated JSON endpoint.

        `results_key` is the path to each page's list of records, with
        fallbacks such as "results|data|items". results_key=None means the
        body itself is the list. `count_key` is the optional path to the
        total record count, such as "count". It shows in the progress log,
        and lets Offset stop exactly at the end. `paging` says how to reach
        the next page: NextLink() (the default) follows a next-page URL in
        each page, and Offset(...) steps a limit/offset query. `params` are
        added to the first URL; later pages carry or work out their own.
        `model` turns each record into a typed object (see converter): a
        pydantic model, msgspec Struct, dataclass or function.

        Each page logs one INFO line on the "api_explorer.client" logger,
        such as "Page 44: 100 records in 0.62s, 4,400 so far of 61,203", so
        a long run can be watched (the explorer notebook shows them). The
        record also carries the numbers as record.progress, a Progress, for
        a progress bar or other display. Retries log as warnings.

        records() is built from public parts, so a loop of your own can
        reuse them: paginate() follows the pages, a Paging finds the next
        one, read_page() finds the records and the count, and converter()
        applies the model.

        Raises ApiError when a request fails, when the results path is
        missing or isn't a list, when a record doesn't fit `model`, or when
        paging finds something wrong. A
        missing results path is an error, not an empty page, so a mistyped
        key can't pass for "no data". An explicit null is an empty page.
        """
        url = httpx2.URL(self.url(endpoint)).copy_merge_params(params or {})
        convert = None if model is None else converter(model)
        name = getattr(model, "__name__", repr(model))
        warned_no_count = False

        def read(page: Any) -> Page:
            nonlocal warned_no_count
            result = read_page(page, results_key, count_key, url=str(url))
            if count_key is not None and result.total is None and not warned_no_count:
                warned_no_count = True
                logger.warning("No count at %r in the response", count_key)
            return result

        seconds = 0.0

        def fetch(page_url: str) -> Any:
            nonlocal seconds
            started = time.monotonic()
            page = self.get_json(page_url)
            seconds = time.monotonic() - started
            return page

        first, next_link = (paging or NextLink()).begin(str(url), read)
        received = 0
        for number, page in enumerate(
            paginate(fetch, first, next_link, max_pages=max_pages), start=1
        ):
            items, total = read(page)
            received += len(items)
            of_total = "" if total is None else f" of {total:,}"
            progress = Progress(number, len(items), received, total, seconds)
            logger.info(
                "Page %d: %d records in %.2fs, %s so far%s",
                number,
                len(items),
                seconds,
                f"{received:,}",
                of_total,
                extra={"progress": progress},
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
