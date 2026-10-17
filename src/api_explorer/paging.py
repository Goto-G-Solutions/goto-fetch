"""Follow a paginated API from page to page: the loop, and the paging styles."""

import dataclasses
import json
import logging
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, Protocol, TypeVar
from urllib.parse import urljoin

import httpx2

from .errors import ApiError
from .logs import log_event
from .paths import MISSING, get_path

logger = logging.getLogger(__name__)


def _stopped(why: str, reason: str) -> None:
    """Log, at DEBUG, why a paging style ended a run."""
    log_event(
        logger, logging.DEBUG, "stop", "Stopped: %s", why, data={"reason": reason}
    )


R = TypeVar("R")
"""What paginate fetches by: a URL, a request body, a cursor."""
P = TypeVar("P")
"""What fetch returns for one page."""


def request_key(request: Any) -> Hashable:
    """Tell requests apart for paginate's loop check: by default, by value.

    A string (a URL) is its own key. Anything else, such as a PageRequest, a
    dict holding a POST body, or a cursor, is keyed by its JSON form, with
    keys sorted, so the same body written in another order is the same page.
    """
    if isinstance(request, str):
        return request
    if dataclasses.is_dataclass(request) and not isinstance(request, type):
        request = dataclasses.asdict(request)
    return json.dumps(request, sort_keys=True, default=repr)


def paginate(
    fetch: Callable[[R], P],
    first: R,
    next_request: Callable[[P, R], R | None],
    *,
    max_pages: int | None = None,
    key: Callable[[R], Hashable] = request_key,
) -> Iterator[P]:
    """Yield pages one at a time, following each page to the next.

    fetch(request) returns a page, and next_request(page, request) says
    what to fetch next, or None at the end. A request is whatever your
    fetch needs: usually a URL, but a POST body or a cursor works too. When
    both are strings, a relative next URL resolves against the current one.
    Pages are fetched only as the caller asks for them, and at most
    `max_pages` of them.

    Raises ApiError if the next request was already fetched, since following
    it would loop forever. Requests are told apart by key(request); see
    request_key for the default, and pass your own when only part of a
    request identifies the page.
    """
    seen: set[Hashable] = set()
    request: R | None = first
    while request is not None and (max_pages is None or len(seen) < max_pages):
        identity = key(request)
        if identity in seen:
            url = request if isinstance(request, str) else None
            raise ApiError(
                f"Pagination loops: {request!r} was already fetched", url=url
            )
        seen.add(identity)
        page = fetch(request)
        yield page
        following = next_request(page, request)
        if isinstance(following, str) and isinstance(request, str):
            following = urljoin(request, following) if following else None
        request = following or None


class Page(NamedTuple):
    """One page as records() reads it.

    `items` are its records, already checked to be a list. `total` is the
    record count the page reports, or None if it reports none.
    """

    items: list
    total: int | None


def read_page(
    page: Any,
    results_key: str | None = "results",
    count_key: str | None = None,
    *,
    url: str | None = None,
) -> Page:
    """Read one page of a paginated JSON API: its records, and its count if any.

    `results_key` is the path to the records (see get_path), with fallbacks
    such as "results|items"; None means the page itself is the list. An
    explicit null there is an empty page. `count_key` is the path to the
    total record count; a value that isn't a whole number counts as no
    count.

    Raises ApiError, naming `url` if given, when the results path is missing
    or doesn't hold a list. Missing is an error, not an empty page, so a
    mistyped key can't pass for "no data".
    """
    items = page if results_key is None else get_path(page, results_key, MISSING)
    if items is MISSING:
        raise ApiError(f"No {results_key!r} in the response", url=url)
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ApiError(
            f"{results_key!r} holds a {type(items).__name__}, not a list", url=url
        )
    total = None if count_key is None else get_path(page, count_key)
    if not isinstance(total, int) or isinstance(total, bool):
        total = None
    return Page(items, total)


@dataclass(frozen=True)
class PageRequest:
    """The request for one page: its URL, method, and JSON body if any.

    Client.records starts from one (records(method=..., json=...)) and
    paging styles say what the next one is.
    """

    url: str
    method: str = "GET"
    json: Any = None


class Fetched(NamedTuple):
    """One page as Client.records fetched it: the parsed body, and its response.

    Paging styles get these, so they can read the next page from the body
    (NextLink), the query (Offset) or the headers (HeaderLink).
    """

    body: Any
    response: httpx2.Response


NextLinkFunction = Callable[[Fetched, PageRequest], "PageRequest | str | None"]
"""Given a page and its request, what to fetch next: see Paging."""


class Paging(Protocol):
    """How a paginated JSON API says where the next page is.

    begin(first, read) starts one run, from the first PageRequest. It
    returns the request to fetch first and a fresh next_link(fetched,
    request), which gets each page as a Fetched and says what comes next:

    - a URL (str), fetched with the same method and body, so styles that
      work on URLs work for POST searches too; a relative URL resolves
      against the current one;
    - a PageRequest, to change the method or the body;
    - None, at the end.

    read(fetched) gives the page's records and count, as read_page does.
    """

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]: ...


@dataclass(frozen=True)
class NextLink:
    """Follow a next-page URL that each page carries, at path `key`.

    This is Django REST Framework's style, for page numbers and for
    limit/offset alike: {"results": [...], "next": "...?page=2"}.
    """

    key: str = "next"

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run; the first request is used as given."""

        def next_link(fetched: Fetched, request: PageRequest) -> str | None:
            link = get_path(fetched.body, self.key)
            if link is not None and not isinstance(link, str):
                kind = type(link).__name__
                raise ApiError(
                    f"{self.key!r} holds a {kind}, not a URL", url=request.url
                )
            if not link:
                _stopped("the page has no next link", "no next link")
            return link

        return first, next_link


@dataclass(frozen=True)
class Offset:
    """Step a limit/offset query, for APIs whose pages carry no next link.

    This is Django Ninja's default style: {"items": [...], "count": 250}.
    Each request asks for `limit` records, and the offset grows by the
    records actually received, so a server that caps the page size below
    `limit` loses nothing. A run starts at the offset already in the URL, or
    at 0.

    The run ends at the first of these signals, best first:

    1. The page's count, when records() is given a count_key and the page
       has one: stop once the records reach it. If the records run out
       first, a warning gives both numbers, because the data probably
       changed while paging.
    2. A page shorter than the biggest page so far. Not shorter than
       `limit`, which a capped server would trip on every page.
    3. An empty page. It catches a total that fits on the first page or is
       an exact multiple of the page size, at the cost of one more request.

    A page that repeats the one before raises ApiError: the server is
    probably ignoring the offset, and would otherwise page forever.
    """

    limit: int = 100
    limit_param: str = "limit"
    offset_param: str = "offset"

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"limit must be at least 1, got {self.limit}")

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run: set limit and offset on the first request's URL."""
        url = httpx2.URL(first.url)
        start = int(url.params.get(self.offset_param, 0))
        start_url = url.copy_set_param(self.limit_param, self.limit)
        start_url = start_url.copy_set_param(self.offset_param, start)
        received = 0
        biggest = 0
        previous: list | None = None

        def next_link(fetched: Fetched, request: PageRequest) -> str | None:
            nonlocal received, biggest, previous
            items, total = read(fetched)
            if items and items == previous:
                raise ApiError(
                    f"{request.url} returned the same records as the page "
                    f"before; the server seems to ignore {self.offset_param!r}",
                    url=request.url,
                )
            previous = items
            received += len(items)
            reached = start + received
            if total is not None:
                if reached >= total:
                    _stopped("the records reached the count", "count reached")
                    return None
                if not items:
                    log_event(
                        logger,
                        logging.WARNING,
                        "count-short",
                        "Records ran out at %d of the %d the count promised",
                        reached,
                        total,
                        data={"records": reached, "count": total},
                    )
                    return None
            elif not items:
                _stopped("a page came back empty", "empty page")
                return None
            elif len(items) < biggest:
                _stopped("a page was shorter than the biggest before it", "short page")
                return None
            biggest = max(biggest, len(items))
            return str(
                httpx2.URL(request.url).copy_set_param(self.offset_param, reached)
            )

        return dataclasses.replace(first, url=str(start_url)), next_link


@dataclass(frozen=True)
class HeaderLink:
    """Follow the next-page link in each response's Link header.

    This is GitHub's style, among others: the body holds only the records,
    and the response carries `Link: <...?page=2>; rel="next"`. `rel` names
    the link to follow.
    """

    rel: str = "next"

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run; the first request is used as given."""

        def next_link(fetched: Fetched, request: PageRequest) -> str | None:
            link = fetched.response.links.get(self.rel, {}).get("url")
            if not link:
                _stopped("the response has no next link", "no next link")
            return link

        return first, next_link


@dataclass(frozen=True)
class Cursor:
    """Send back the cursor each page gives, until a page gives none.

    `key` is the path to the next cursor in the body, such as "next_cursor"
    or "meta.next". `param` is the name it goes back under, and `send_in`
    says where: "query" (?cursor=abc) or "body" (a field in the JSON body,
    for POST searches).
    """

    key: str = "next_cursor"
    param: str = "cursor"
    send_in: Literal["query", "body"] = "query"

    def __post_init__(self) -> None:
        if self.send_in not in ("query", "body"):
            raise ValueError(f"send_in must be 'query' or 'body', got {self.send_in!r}")

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run; the first request is used as given."""

        def next_link(fetched: Fetched, request: PageRequest) -> PageRequest | None:
            cursor = get_path(fetched.body, self.key)
            if cursor is None or cursor == "":
                _stopped("the page has no cursor", "no cursor")
                return None
            if self.send_in == "query":
                url = httpx2.URL(request.url).copy_set_param(self.param, cursor)
                return dataclasses.replace(request, url=str(url))
            body = dict(request.json or {})
            body[self.param] = cursor
            return dataclasses.replace(request, json=body)

        return first, next_link
