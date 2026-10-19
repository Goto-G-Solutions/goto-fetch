"""Scraping: the CSS and XPath field extractors, and scrape() across pages."""

import pytest

from goto_fetch import http

pytest.importorskip("parsel", reason='needs the "scrape" extra')

from parsel import Selector

from goto_fetch.scrape import CSSField, ScraperConfig, XPathField, scrape

PAGE_1 = """
<article class="post"><h2><a href="/post/1">First</a></h2><span class="author">Ann</span></article>
<article class="post"><h2><a href="/post/2">Second</a></h2></article>
<a class="next" href="/page/2">Next</a>
"""
PAGE_2 = '<article class="post"><h2><a href="/post/3">Third</a></h2></article>'
CONFIG = ScraperConfig(
    item_selector="article.post",
    next_page_selector="a.next",
    fields={
        "title": CSSField("h2 a"),
        "url": CSSField("h2 a", extract="href"),
        "author": CSSField(".author"),
    },
)


def element(html: str) -> Selector:
    return Selector(text=f"<div>{html}</div>").css("div")[0]


@pytest.mark.parametrize(
    ("field", "html", "value"),
    [
        (CSSField("h2"), "<h2>  Hello  </h2>", "Hello"),
        (CSSField("a", extract="href"), "<a href='/p'>x</a>", "/p"),
        (CSSField(".missing"), "<h2>Hello</h2>", None),
        (XPathField(".//h2/text()"), "<h2> Hello </h2>", "Hello"),
        (XPathField(".//span/text()", strip=False), "<span>  pad  </span>", "  pad  "),
        (XPathField(".//a/@href"), "<a href='/p'>x</a>", "/p"),
        (XPathField("normalize-space(.//p/text())"), "<p>  a   b  </p>", "a b"),
        (XPathField(".//missing/text()"), "<h2>Hello</h2>", None),
    ],
)
def test_field_extractors_read_text_or_attributes(field, html, value):
    assert field.extract_value(element(html)) == value


def html_at(pages: dict[str, str]):
    """A handler that serves HTML by URL path, and its call log."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return http.Response(200, text=pages.get(request.url.path, ""))

    return handler, calls


def test_scrape_yields_one_row_per_item(mock_client):
    handler, _ = html_at({"/page/1": PAGE_1})
    client = mock_client(handler)
    rows = list(scrape(client, "http://site.test/page/1", CONFIG, max_pages=1))
    assert rows == [
        {"title": "First", "url": "/post/1", "author": "Ann"},
        {"title": "Second", "url": "/post/2", "author": None},
    ]


def test_scrape_follows_next_links_until_there_are_none(mock_client):
    handler, calls = html_at({"/page/1": PAGE_1, "/page/2": PAGE_2})
    client = mock_client(handler)
    rows = list(scrape(client, "http://site.test/page/1", CONFIG))
    assert [r["title"] for r in rows] == ["First", "Second", "Third"]
    assert calls == ["/page/1", "/page/2"]


def test_scrape_can_select_a_value_by_its_sibling_label(mock_client):
    # Overlaps the XPath extractor tests on purpose: this is the case XPath
    # exists for (CSS can't select by a sibling's text), checked end to end.
    html = """<article><table>
      <tr><td class="key">Author</td><td class="val">Ann</td></tr>
      <tr><td class="key">Date</td><td class="val">2024-01</td></tr>
    </table></article>"""
    config = ScraperConfig(
        item_selector="article",
        fields={
            "author": XPathField(
                ".//td[@class='val'][preceding-sibling::td[text()='Author']]/text()"
            ),
            "date": XPathField(
                ".//td[@class='val'][preceding-sibling::td[text()='Date']]/text()"
            ),
        },
    )
    handler, _ = html_at({"/": html})
    rows = list(scrape(mock_client(handler), "http://site.test/", config))
    assert rows == [{"author": "Ann", "date": "2024-01"}]
