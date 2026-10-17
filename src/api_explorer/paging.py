"""Follow a paginated API from page to page: the loop, and the paging styles."""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol
from urllib.parse import urljoin

import httpx2

from .errors import ApiError
from .paths import get_path

logger = logging.getLogger(__name__)


def paginate(
    fetch: Callable[[str], Any],
    first_url: str,
    next_link: Callable[[Any, str], str | None],
    *,
    max_pages: int | None = None,
) -> Iterator[Any]:
    """Yield pages one at a time, following each page to the next.

    fetch(url) returns a page. next_link(page, url) returns the next page's
    URL, or None at the end; it gets the current URL too, so a link can be
    worked out from it as well as read from the page. A relative link
    resolves against the current URL. Pages are fetched only as the caller
    asks for them, and at most `max_pages` of them.

    Raises ApiError if a link leads back to a page already fetched, since
    following it would loop forever.
    """
    seen: set[str] = set()
    url: str | None = first_url
    while url is not None and (max_pages is None or len(seen) < max_pages):
        if url in seen:
            raise ApiError(f"Pagination loops: {url} was already fetched", url=url)
        seen.add(url)
        page = fetch(url)
        yield page
        link = next_link(page, url)
        url = urljoin(url, link) if link else None


class Page(NamedTuple):
    """One page as records() reads it.

    `items` are its records, already checked to be a list. `total` is the
    record count the page reports, or None if it reports none.
    """

    items: list
    total: int | None


class Paging(Protocol):
    """How a paginated JSON API says where the next page is.

    begin(first_url, read) starts one run. It returns the URL to fetch first
    and a fresh next_link(page, url) for paginate. read(page) turns a raw
    page into a Page.
    """

    def begin(
        self, first_url: str, read: Callable[[Any], Page]
    ) -> tuple[str, Callable[[Any, str], str | None]]: ...


@dataclass(frozen=True)
class NextLink:
    """Follow a next-page URL that each page carries, at path `key`.

    This is Django REST Framework's style, for page numbers and for
    limit/offset alike: {"results": [...], "next": "...?page=2"}.
    """

    key: str = "next"

    def begin(
        self, first_url: str, read: Callable[[Any], Page]
    ) -> tuple[str, Callable[[Any, str], str | None]]:
        """Start a run; the first URL is used as given."""

        def next_link(page: Any, url: str) -> str | None:
            link = get_path(page, self.key)
            if link is not None and not isinstance(link, str):
                kind = type(link).__name__
                raise ApiError(f"{self.key!r} holds a {kind}, not a URL", url=url)
            return link

        return first_url, next_link


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
        self, first_url: str, read: Callable[[Any], Page]
    ) -> tuple[str, Callable[[Any, str], str | None]]:
        """Start a run: set limit and offset on the first URL."""
        url = httpx2.URL(first_url)
        start = int(url.params.get(self.offset_param, 0))
        first = url.copy_set_param(self.limit_param, self.limit)
        first = first.copy_set_param(self.offset_param, start)
        received = 0
        biggest = 0
        previous: list | None = None

        def next_link(page: Any, page_url: str) -> str | None:
            nonlocal received, biggest, previous
            items, total = read(page)
            if items and items == previous:
                raise ApiError(
                    f"{page_url} returned the same records as the page "
                    f"before; the server seems to ignore {self.offset_param!r}",
                    url=page_url,
                )
            previous = items
            received += len(items)
            reached = start + received
            if total is not None:
                if reached >= total:
                    return None
                if not items:
                    logger.warning(
                        "Records ran out at %d of the %d the count promised",
                        reached,
                        total,
                    )
                    return None
            elif not items or len(items) < biggest:
                return None
            biggest = max(biggest, len(items))
            return str(httpx2.URL(page_url).copy_set_param(self.offset_param, reached))

        return str(first), next_link
