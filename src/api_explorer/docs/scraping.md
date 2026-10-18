<!-- needs: parsel -->
# Scraping

For pages without an API, `api_explorer.scrape` (the `scrape` extra) follows "next" links through HTML, with the same paging loop, retries and auth as JSON.

## Examples

```python
from api_explorer import http
from api_explorer import Client
from api_explorer.scrape import CSSField, ScraperConfig, XPathField, scrape
from api_explorer.testing import FakeApi

pages = {
    "/shop/": '<article class="product"><h2>Mug</h2><a href="/mug">x</a></article>'
    '<a class="next" href="/shop/2">next</a>',
    "/shop/2": '<article class="product"><h2>Tea</h2><a href="/tea">x</a></article>',
}
client = Client(
    "http://shop.test",
    trailing_slash=False,
    transport=FakeApi(lambda r: http.Response(200, text=pages[r.url.path])),
)

config = ScraperConfig(
    item_selector="article.product",
    fields={
        "name": CSSField("h2"),
        "link": CSSField("a", extract="href"),
        "name_again": XPathField(".//h2/text()"),
    },
    next_page_selector="a.next",  # its href leads to the next page
)
rows = list(scrape(client, "/shop/", config))
assert rows == [
    {"name": "Mug", "link": "/mug", "name_again": "Mug"},
    {"name": "Tea", "link": "/tea", "name_again": "Tea"},
]
```

A field of your own is any object with `extract_value(element)`; see [build-your-own](build-your-own.md).

## Traps

- Respect a site's terms and `robots.txt`, and keep a modest pace.
- A next link that leads back to a page already seen raises `PaginationLoop`.
