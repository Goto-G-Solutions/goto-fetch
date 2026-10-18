# Testing

`goto_fetch.testing` has fake APIs that page, filter, fail and authenticate like real ones, in memory. Each fake is a `Transport`, so it goes straight into `Client(transport=...)`: code that fetches is tested with any test framework and without the network. `serve(api)` puts a fake behind a real server on this machine, to test a real transport against it.

## The fakes

| Fake | Behaves like |
|------|--------------|
| `next_link_api(records, page_size=)` | Django REST Framework: `count`, `next`, `results` |
| `offset_api(records, cap=, count=)` | Django Ninja: `?limit=&offset=`, with an optional server cap and count |
| `cursor_api(records, send_in=)` | a cursor in the query string or a POST body |
| `link_header_api(records)` | GitHub: a bare list, with a `Link` header to the next page |
| `changes_api(records, field=, param=)` | filters by a field from a value on, for `Incremental` |
| `keyset_api(records, field=, param=)` | sorted by a field, paged from a value with `?limit=`, for `Keyset` |
| `TokenServer()` | simplejwt: login, refresh, rotation, expiry, client credentials |
| `json_pages(pages)`, `replies(*responses)` | fixed pages by URL, or fixed responses in turn |
| `flaky(api, 503, ...)` | any of these, failing first as told |

`records` is a list, or a number n for the records 0 to n-1; `changes_api` and `keyset_api` take a list of dicts, since they read a field of each. Both read their list on every request, so a test can change the data between runs, or during one.

Each returns a `FakeApi`, which remembers every request (`.calls`, `.paths`, `.queries`, `.bodies`). `.client()` gives a `Client` wired to it, whose retries don't wait.

## Examples

```python
from goto_fetch import Offset
from goto_fetch.testing import flaky, offset_api


def test_my_pipeline_survives_a_capped_server_and_a_hiccup():
    api = flaky(offset_api(250, cap=100), 503)  # one 503, then pages of at most 100
    records = list(
        api.client().records("/items/", results_key="items", paging=Offset(limit=1000))
    )
    assert records == list(range(250))
    assert len(api.calls) == 4  # the 503, then 3 pages


test_my_pipeline_survives_a_capped_server_and_a_hiccup()
```

A client set up elsewhere, with its own auth or settings, takes the fake as its transport:

```python
from goto_fetch import Client, http
from goto_fetch.testing import replies

api = replies(http.Response(200, json={"results": [1], "next": None}))
client = Client("http://api.test/api", transport=api)
assert list(client.records("/items/")) == [1]
assert api.paths == ["/api/items/"]
```

`MemoryState` and `MemoryWarehouse` stand in for a state store and a warehouse, with nothing on disk.

## Checking pieces of your own

Each interface with promises the types can't state has a check, which raises `AssertionError` naming the first promise broken: `check_paging`, `check_retry_policy`, `check_tracker`, `check_state_store`, `check_warehouse`, `check_transport`. `fetched(body, headers=...)` builds the page a paging style receives, to test a style on its own. See [build-your-own](build-your-own.md).

`serve(api)` puts a fake behind a real HTTP server on this machine, for testing a real transport, or code that only takes a URL:

```python
from goto_fetch import Urllib3Transport
from goto_fetch.testing import next_link_api, serve

with serve(next_link_api(15, page_size=10)) as base:
    over_http = Client(f"{base}/api", transport=Urllib3Transport())
    assert len(list(over_http.records("/items/"))) == 15
```
