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
from .paging import NextLink, Page, Paging, paginate
from .paths import MISSING, get_path
from .retry import Retry, retry_after_seconds

logger = logging.getLogger(__name__)


class Client:
    """An HTTP client for one API.

    Relative endpoints join onto `base_url`: "/items/" under
    "https://host/api" is "https://host/api/items/". Absolute URLs, such as
    the next-page links APIs return, are used as given. With
    `trailing_slash`, a relative endpoint gets a "/" before any query, as
    Django REST Framework expects.

    `retry` says when a failed request is tried again (see Retry): by
    default up to 3 tries for network errors, timeouts, rate limits and
    temporary server errors, for idempotent methods only. `decode` parses
    JSON bodies (see json_decoder); by default msgspec when installed.

    `auth` signs every request, for tokens that change, such as
    TokenRefresh. Its own URLs (login, refresh) follow the endpoint rules.
    A fixed token can go in `headers` instead (token_headers).

    Use it in a `with` block, or call close() when done.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 20.0,
        retry: Retry | None = None,
        decode: Decoder | None = None,
        trailing_slash: bool = True,
        auth: httpx2.Auth | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.retry = retry or Retry()
        self.decode = decode or json_decoder()
        self.trailing_slash = trailing_slash
        self._http = httpx2.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            timeout=timeout,
            follow_redirects=True,
            auth=auth,
            transport=transport,
        )
        # An auth with URLs of its own (TokenRefresh) resolves them the way
        # endpoints are resolved, so one rule covers every URL.
        bind = getattr(auth, "bind", None)
        if callable(bind):
            bind(self.url)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connections."""
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

        `retry` overrides whether this call is retried, which by default
        depends on the method (Retry.methods). Other keyword arguments go to
        httpx2: params=, json=, data=, headers= and so on.
        """
        method = method.upper()
        url = self.url(endpoint)
        policy = self.retry
        if retry is None:
            retry = method in policy.methods
        tries = policy.attempts if retry else 1
        attempt = 1
        while True:
            wait = 0.0
            try:
                response = self._http.request(method, url, **kwargs)
            except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
                failure = ApiError(f"{method} {url} failed: {exc}", url=url)
                failure.__cause__ = exc
            except httpx2.HTTPError as exc:
                raise ApiError(f"{method} {url} failed: {exc}", url=url) from exc
            else:
                if response.is_success:
                    return response
                status = response.status_code
                failure = ApiError(
                    f"{method} {response.url} returned HTTP {status}: "
                    f"{response.text[:300]}",
                    url=str(response.url),
                    status=status,
                )
                if status not in policy.statuses:
                    raise failure
                wait = retry_after_seconds(response.headers.get("Retry-After")) or 0.0
                if wait > policy.max_backoff:
                    raise ApiError(
                        f"{failure} (the server asks to wait {wait:.0f}s, "
                        f"more than max_backoff={policy.max_backoff:g}s)",
                        url=failure.url,
                        status=status,
                    )
            if attempt >= tries:
                raise failure
            delay = max(wait, policy.delay(attempt - 1))
            logger.warning(
                "%s; retry %d of %d in %.1fs",
                failure,
                attempt,
                tries - 1,
                delay,
            )
            time.sleep(delay)
            attempt += 1

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
        a long run can be watched (the explorer notebook shows them).
        Retries log as warnings.

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
            items = (
                page if results_key is None else get_path(page, results_key, MISSING)
            )
            if items is MISSING:
                raise ApiError(f"No {results_key!r} in the response", url=str(url))
            if items is None:
                items = []
            if not isinstance(items, list):
                kind = type(items).__name__
                raise ApiError(
                    f"{results_key!r} holds a {kind}, not a list",
                    url=str(url),
                )
            if count_key is None:
                return Page(items, None)
            total = get_path(page, count_key)
            if isinstance(total, int) and not isinstance(total, bool):
                return Page(items, total)
            if not warned_no_count:
                warned_no_count = True
                logger.warning("No count at %r in the response", count_key)
            return Page(items, None)

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
            logger.info(
                "Page %d: %d records in %.2fs, %s so far%s",
                number,
                len(items),
                seconds,
                f"{received:,}",
                of_total,
                extra={"progress": True},
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
