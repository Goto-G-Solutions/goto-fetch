"""Follow a paginated API from page to page: the loop, and the paging styles."""

import dataclasses
import json
import logging
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, Protocol, TypeVar
from urllib.parse import urljoin

from .errors import ApiError
from .http import URL, Response, set_param
from .logs import log_event
from .ordering import greater, same, send_form
from .paths import MISSING, get_path

logger = logging.getLogger(__name__)


def _stopped(why: str, reason: str) -> None:
    """Log, at DEBUG, why a paging style ended a run."""
    log_event(
        logger, logging.DEBUG, "stop", "Stopped: %s", why, data={"reason": reason}
    )


class PaginationLoop(ApiError):
    """Raised when paging asks for a page it already fetched, which would loop forever."""


R = TypeVar("R")
"""What paginate fetches by: a URL, a request body, a cursor."""
P = TypeVar("P")
"""What fetch returns for one page."""


def with_query(url: str, name: str, value: object) -> str:
    """Return `url` with query parameter `name` set to `value`, others kept.

    The everyday step of a paging style: with_query(request.url, "page", 3).
    A URL returned from a style keeps the request's method and body (see
    Paging).
    """
    return set_param(url, name, value)


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
            # Named by URL, not repr: a repr would show a request body,
            # which redaction (URLs only) can't clean.
            url = request if isinstance(request, str) else getattr(request, "url", None)
            raise PaginationLoop(
                f"Pagination loops: {url or 'a request'} was already fetched", url=url
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
    explicit null there is an empty page, and so is an empty body (page
    None, such as a 204). `count_key` is the path to the total record count:
    a whole number, or digits as text; anything else counts as no count.

    Raises ApiError, naming `url` if given, when the results path is missing
    or doesn't hold a list. Missing is an error, not an empty page, so a
    mistyped key can't pass for "no data".
    """
    if page is None:
        return Page([], None)  # an empty body, such as a 204 No Content
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
    if isinstance(total, str) and total.strip().isdigit():
        total = int(total)  # some APIs send the count as text, "250"
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


def with_param(
    request: PageRequest,
    name: str,
    value: object,
    send_in: Literal["query", "body"] = "query",
) -> PageRequest:
    """Return `request` with `name` set to `value`, in its query or its JSON body.

    The step paging styles and trackers share: a cursor, a page size or a
    "from this value" filter. In the body, `name` joins the request's JSON
    object, which must be an object (or no body at all).
    """
    check_send_in(send_in)
    if send_in == "query":
        return dataclasses.replace(request, url=with_query(request.url, name, value))
    if request.json is not None and not isinstance(request.json, dict):
        raise ApiError(
            f"Can't add {name!r} to a body that isn't a JSON object: "
            f"{json.dumps(request.json, default=str)[:200]}",
            url=request.url,
        )
    return dataclasses.replace(request, json={**(request.json or {}), name: value})


class Fetched(NamedTuple):
    """One page as Client.records fetched it: the parsed body, and its response.

    Paging styles get these, so they can read the next page from the body
    (NextLink), the query (Offset) or the headers (HeaderLink).
    """

    body: Any
    response: Response


NextLinkFunction = Callable[[Fetched, PageRequest], "PageRequest | str | None"]
"""Given a page and its request, what to fetch next: see Paging."""


def follow(
    next_link: NextLinkFunction,
) -> Callable[[Fetched, PageRequest], "PageRequest | None"]:
    """paginate's next_request, from a Paging's next_link.

    Applies the rule every Paging relies on: a URL (str) keeps the
    request's method and body, a relative one resolves against the current
    URL, and an empty one or None ends the run; a PageRequest is used as
    is. Client.records follows pages this way, and so can a loop of your
    own: paginate(fetch, first, follow(next_link)).
    """

    def next_request(fetched: Fetched, request: PageRequest) -> PageRequest | None:
        following = next_link(fetched, request)
        if isinstance(following, str):
            if not following:
                return None
            return dataclasses.replace(request, url=urljoin(request.url, following))
        return following

    return next_request


def check_send_in(send_in: str) -> None:
    """Refuse a place to send a parameter other than "query" or "body"."""
    if send_in not in ("query", "body"):
        raise ValueError(f"send_in must be 'query' or 'body', got {send_in!r}")


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


class _FollowRules:
    """What NextLink, HeaderLink and Cursor share, for one run.

    - An empty page ends the run, unless the style has stop_on_empty=False:
      some APIs keep sending a next link or cursor after the data ends, and
      would otherwise page forever.
    - A next-page path missing from the first page (not null: missing) logs
      a warning once, since it usually means a mistyped path, such as an
      unquoted "@odata.nextLink"; the run then stops after that page.
    """

    def __init__(self, read: Callable[[Fetched], Page], stop_on_empty: bool) -> None:
        self.read = read
        self.stop_on_empty = stop_on_empty
        self.first = True

    def empty(self, fetched: Fetched) -> bool:
        """True when this page has no records and that should end the run."""
        if self.stop_on_empty and not self.read(fetched).items:
            _stopped("a page came back empty", "empty page")
            return True
        return False

    def check_first(self, fetched: Fetched, key: str) -> None:
        """Warn once if `key` is missing from the run's first page."""
        missing = (
            fetched.body is not None and get_path(fetched.body, key, MISSING) is MISSING
        )
        if self.first and missing:
            log_event(
                logger,
                logging.WARNING,
                "no-next-key",
                "No %r in the first page, so paging stops after it; check the path "
                "(keys holding dots go in quotes, such as '[\"@odata.nextLink\"]')",
                key,
                data={"key": key},
            )
        self.first = False


@dataclass(frozen=True)
class NextLink:
    """Follow a next-page URL that each page carries, at path `key`.

    This is Django REST Framework's style, for page numbers and for
    limit/offset alike: {"results": [...], "next": "...?page=2"}. An empty
    page ends the run even if it carries a next link, unless
    stop_on_empty=False (for APIs that send empty pages mid-run).
    """

    key: str = "next"
    stop_on_empty: bool = True

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run; the first request is used as given."""
        rules = _FollowRules(read, self.stop_on_empty)

        def next_link(fetched: Fetched, request: PageRequest) -> str | None:
            rules.check_first(fetched, self.key)
            link = get_path(fetched.body, self.key)
            # A malformed link is an error even on an empty page.
            if link is not None and not isinstance(link, str):
                kind = type(link).__name__
                raise ApiError(
                    f"{self.key!r} holds a {kind}, not a URL", url=request.url
                )
            if rules.empty(fetched):
                return None
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

    Offsets shift when records are added or removed during a run, so a
    record can be skipped or seen twice. Where the API offers it, prefer a
    next link or cursor, or sort oldest first so new records land at the
    end. A "count-short" warning is often the first sign.

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
        """Start a run: set limit and offset on the first request's query."""
        start = int(URL(first.url).params.get(self.offset_param, 0))
        begun = with_param(first, self.limit_param, self.limit)
        begun = with_param(begun, self.offset_param, start)
        received = 0
        biggest = 0
        previous: list | None = None

        def next_link(fetched: Fetched, request: PageRequest) -> PageRequest | None:
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
            return with_param(request, self.offset_param, reached)

        return begun, next_link


@dataclass(frozen=True)
class HeaderLink:
    """Follow the next-page link in each response's Link header.

    This is GitHub's style, among others: the body holds only the records,
    and the response carries `Link: <...?page=2>; rel="next"`. `rel` names
    the link to follow. An empty page ends the run, unless
    stop_on_empty=False.
    """

    rel: str = "next"
    stop_on_empty: bool = True

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run; the first request is used as given."""
        rules = _FollowRules(read, self.stop_on_empty)

        def next_link(fetched: Fetched, request: PageRequest) -> str | None:
            if rules.empty(fetched):
                return None
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
    for POST searches). An empty page ends the run even if it carries a
    cursor, unless stop_on_empty=False. A cursor must be one value, text or
    a whole number; a path that finds several raises ApiError.
    """

    key: str = "next_cursor"
    param: str = "cursor"
    send_in: Literal["query", "body"] = "query"
    stop_on_empty: bool = True

    def __post_init__(self) -> None:
        check_send_in(self.send_in)

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run; the first request is used as given."""
        rules = _FollowRules(read, self.stop_on_empty)

        def next_link(
            fetched: Fetched, request: PageRequest
        ) -> PageRequest | str | None:
            rules.check_first(fetched, self.key)
            cursor = get_path(fetched.body, self.key)
            # A malformed cursor is an error even on an empty page.
            one_value = isinstance(cursor, (str, int)) and not isinstance(cursor, bool)
            if cursor is not None and not one_value:
                kind = type(cursor).__name__
                raise ApiError(
                    f"{self.key!r} holds a {kind}, not one cursor value",
                    url=request.url,
                )
            if rules.empty(fetched):
                return None
            if cursor is None or cursor == "":
                _stopped("the page has no cursor", "no cursor")
                return None
            return with_param(request, self.param, cursor, self.send_in)

        return first, next_link


@dataclass(frozen=True)
class Keyset:
    """Page by the records' own field: each page asks from the last value of the one before.

    For an API that sorts by a field that only grows, such as updated_at
    (often with a parameter like ordering=updated_at), and filters by it
    from a value on (`param`, such as updated_since). Page numbers and
    offsets skip a record when the data changes during a run; keyset pages
    don't, because each page starts from a value, not a position.

    Pages overlap: each starts at the last value of the one before, so the
    records at that value come again. Pair Keyset with Incremental or
    Changes, which pass the repeats over; on its own it yields them again.

    - `field`: path to the sorted value in each record.
    - `param`: the filter parameter, "from this value on" (>=). One that
      means "after this value" (>) would skip records that share a value.
    - `limit`, `limit_param`: the page size it asks for, and its name.
      Keyset needs it to tell the last page from a page full of one value.
      At least 2, since each page repeats the last record of the one before.
    - `send_in`, `send_as`: where the parameters go, and the form of the
      value, as for Incremental. A coarser form, such as the date of a
      time, widens the overlap to every record since that date: they come
      again (store with a key, so they replace their rows), and a page
      can't move past a date holding more records than `limit`.

    Raises ApiError when a page isn't sorted by `field`, or when more
    records share one value than fit on a page, since it can't move past
    them (raise `limit`), or when the API ignores `param`. A server that
    caps the page size below `limit` is noticed once a short page turns
    out not to be the last; until then, a short page that moves nowhere
    counts as the end, so set `limit` no higher than the server allows.
    """

    field: str
    param: str
    limit: int = 100
    limit_param: str = "limit"
    send_in: Literal["query", "body"] = "query"
    send_as: Callable[[Any], Any] = send_form

    def __post_init__(self) -> None:
        check_send_in(self.send_in)
        if self.limit < 2:
            # Each page after the first starts with the last record of the
            # one before, so a page of one could never move on.
            raise ValueError(
                f"limit must be at least 2 for keyset paging, got {self.limit}"
            )

    def begin(
        self, first: PageRequest, read: Callable[[Fetched], Page]
    ) -> tuple[PageRequest, NextLinkFunction]:
        """Start a run: the first request gets the page size, and keeps any filter."""
        current: Any = None  # the value the last request asked from
        page_size = self.limit  # what the server really gives, if it caps
        previous = 0  # the last page's length

        def next_link(fetched: Fetched, request: PageRequest) -> PageRequest | None:
            nonlocal current, page_size, previous
            items = read(fetched).items
            if not items:
                return None
            bottom, top = self._value_range(items, request)
            # The request asked from send_as(current), which may be coarser
            # (a date for a time), so earlier records from it are fine.
            asked = None if current is None else self.send_as(current)
            if asked is not None and greater(asked, bottom, self.field):
                raise ApiError(
                    f"The page starts at {self.field}={bottom!r}, before the "
                    f"{asked!r} it asked from: the API ignores {self.param!r}",
                    url=request.url,
                )
            following = with_param(request, self.param, self.send_as(top), self.send_in)
            # Asking again from where this page started moves nowhere. The
            # first request may already ask from a value (an Incremental's
            # last), which `current` doesn't know, hence the second test.
            stuck = following == request
            if stuck or (current is not None and same(top, current, self.field)):
                # Only records at the value asked from: the repeats at the
                # end, unless the page is full, when more may follow.
                if len(items) < page_size:
                    return None
                raise ApiError(
                    f"{len(items)} or more records share {self.field}={top!r}, "
                    f"more than a page holds, so Keyset can't page past them; "
                    f"raise limit",
                    url=request.url,
                )
            if current is not None and previous < page_size:
                # The last page was short, yet this one found more: the
                # server caps the page size at what it gave.
                page_size = previous
            current, previous = top, len(items)
            return following

        return with_param(first, self.limit_param, self.limit, self.send_in), next_link

    def _value_range(self, items: list[Any], request: PageRequest) -> tuple[Any, Any]:
        """The page's first and last values of `field`, checking it is sorted by it."""
        first: Any = MISSING
        last: Any = MISSING
        for item in items:
            value = get_path(item, self.field, MISSING)
            if value is MISSING or value is None:
                raise ApiError(
                    f"A record has no {self.field!r}, so Keyset can't page past it",
                    url=request.url,
                )
            if last is not MISSING and greater(last, value, self.field):
                raise ApiError(
                    f"The page isn't sorted by {self.field!r} ({last!r} comes "
                    f"before {value!r}); Keyset needs ascending order, often "
                    f"asked for with a parameter like ordering={self.field}",
                    url=request.url,
                )
            if first is MISSING:
                first = value
            last = value
        return first, last
