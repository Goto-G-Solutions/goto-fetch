# Paging

A paging style tells `client.records()` how to reach the next page. Pick the one that matches what the API sends back.

## Choosing a style

| The API... | Style |
|------------|-------|
| puts the next page's URL in the body (`"next": "https://..."`) | `NextLink()`, the default; `NextLink("links.next")` for another path |
| puts it in a `Link` header, as GitHub does | `HeaderLink()` |
| gives a cursor to send back (`"next_cursor": "abc"`) | `Cursor(key, param, send_in="query" or "body")` |
| takes `?limit=&offset=` and says nothing about the next page | `Offset(limit=100)` |
| sorts by a field that grows and filters "from this value on" | `Keyset(field, param)`, see [incremental](incremental.md) |

## What it handles for you

- **The end:** an empty page ends a run, even when the API keeps sending a next link (`NextLink`, `HeaderLink` and `Cursor` turn that off with `stop_on_empty=False`); `Offset` stops at the count, or at an empty or short page when there is none; `Keyset` at an empty page, or one that moves nowhere.
- **Loops:** asking for a page already fetched raises `PaginationLoop`, rather than looping forever.
- **Server caps:** `Offset` keeps going when the server sends fewer records than asked.
- **Relative links** resolve against the current page, and a link keeps a POST's method and body.
- **A wrong path:** a next-page path missing from the first page logs a warning, since paging would stop after one page.

## Examples

```python
from api_explorer import Cursor, HeaderLink, Offset
from api_explorer.testing import cursor_api, link_header_api, offset_api

# limit/offset, with a server that caps pages at 25 whatever is asked
api = offset_api(120, cap=25)
records = api.client().records(
    "/items/", results_key="items", count_key="count", paging=Offset(limit=100)
)
assert list(records) == list(range(120))

# a bare list, with the next page in the Link header
api = link_header_api(30, page_size=10)
repos = api.client().records("/repos/", results_key=None, paging=HeaderLink())
assert len(list(repos)) == 30

# a POST search with the cursor in the body
api = cursor_api(7, page_size=3, send_in="body")
records = api.client().records(
    "/search/",
    method="POST",
    json={"query": "late orders"},
    results_key="data",
    paging=Cursor(send_in="body"),
    retry=True,  # safe here: this POST only reads
)
assert list(records) == list(range(7))
```

## A loop of your own

`records()` is built from public parts, so a loop that does something else reuses the same rules. `paginate` follows pages by whatever your fetch needs, a style's `begin` gives the first request and a `next_link`, and `follow` turns its answers into the next request:

```python
from api_explorer import Fetched, NextLink, PageRequest, follow, paginate, read_page
from api_explorer.testing import next_link_api

api = next_link_api(25, page_size=10)
client = api.client()


def fetch(request: PageRequest) -> Fetched:
    response = client.request(request.method, request.url, json=request.json)
    return Fetched(client.json_body(response), response)


def read(fetched: Fetched):
    return read_page(fetched.body, "results")


first, next_link = NextLink().begin(PageRequest(client.url("/items/")), read)
sizes = [len(read(page).items) for page in paginate(fetch, first, follow(next_link))]
assert sizes == [10, 10, 5]
```

A style of your own is any object with `begin(first, read)`; see [build-your-own](build-your-own.md).

## Traps

- **Page numbers and offsets over changing data** skip a record when one before it is edited or deleted during the run, as the rest shift a place. That matters most with incremental fetching: prefer a cursor, `Keyset`, or `Changes`.
- **A key holding dots**, such as `@odata.nextLink`, goes in quotes: `NextLink('["@odata.nextLink"]')`.
- `Keyset` needs `limit` of at least 2, since each page repeats the last record of the one before, and no higher than the server allows: until a short page turns out not to be the last, a short page that moves nowhere counts as the end. `send_in="body"` and `send_as=` work as for `Incremental`.
