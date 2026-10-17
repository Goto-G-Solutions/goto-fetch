"""Scrape items from HTML pages, following next-page links.

Needs the "scrape" extra: pip install "api-explorer[scrape]".
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol

from parsel import Selector

from .client import Client
from .paging import paginate


class FieldExtractor(Protocol):
    """Anything that pulls one value out of an item element, such as CSSField."""

    def extract_value(self, element: Selector) -> str | None: ...


@dataclass
class CSSField:
    """Extract a field with a CSS selector: its text, or an attribute."""

    selector: str
    extract: Literal["text"] | str = "text"

    def extract_value(self, element: Selector) -> str | None:
        """Return the stripped text, or the named attribute, of the first match."""
        if self.extract == "text":
            val = element.css(f"{self.selector}::text").get()
            return val.strip() if val else None
        return element.css(f"{self.selector}::attr({self.extract})").get()


@dataclass
class XPathField:
    """Extract a field with an XPath expression.

    The whole expression goes in `selector`, attribute access (`//a/@href`)
    and string functions (`normalize-space(...)`) included. strip=True trims
    the result; use strip=False when the raw value matters.
    """

    selector: str
    strip: bool = True

    def extract_value(self, element: Selector) -> str | None:
        """Return the first match, or None."""
        val = element.xpath(self.selector).get()
        if val is None:
            return None
        return val.strip() if self.strip else val


@dataclass
class ScraperConfig:
    """What to scrape from each page of a site."""

    item_selector: str
    fields: dict[str, FieldExtractor]
    next_page_selector: str | None = None


def scrape(
    client: Client,
    url: str,
    config: ScraperConfig,
    *,
    max_pages: int | None = None,
) -> Iterator[dict[str, str | None]]:
    """Yield one row per item on an HTML page, following next-page links.

    Each element matching config.item_selector becomes a row with one value
    per field in config.fields. The href of the first element matching
    config.next_page_selector leads to the next page. Raises ApiError as
    Client does, and on a pagination loop.
    """

    def fetch(page_url: str) -> Selector:
        return Selector(text=client.request("GET", page_url).text)

    def next_link(page: Selector, page_url: str) -> str | None:
        if not config.next_page_selector:
            return None
        return page.css(config.next_page_selector).attrib.get("href")

    for page in paginate(fetch, client.url(url), next_link, max_pages=max_pages):
        for item in page.css(config.item_selector):
            yield {name: f.extract_value(item) for name, f in config.fields.items()}
