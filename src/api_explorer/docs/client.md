# Client

A `Client` holds what one API needs: its base URL, headers, timeout, retries and authentication. Everything else in the package goes through one.

## When to use it

Always: make one `Client` per API and reuse it. It keeps one connection pool, and it is safe to share across threads.

## What it handles for you

- Joining endpoints to the base URL, with or without a trailing slash (`trailing_slash=True` adds one, as Django REST Framework expects; turn it off for routes that don't end in one).
- Retrying temporary failures (see [retries](retries.md)), and raising one `ApiError` for anything final, with the URL and status, secrets hidden.
- Merging `params=` into a query the endpoint already has.
- Reading JSON: an empty 200 is an error, 204 and 205 are empty, gzip sent without saying so is unpacked, and a non-JSON body names its content type.

## Examples

The examples use a fake API from `api_explorer.testing`, so they run as written; with a real API, give `Client` its URL instead.

```python skip
from api_explorer import Client

with Client("https://api.example.com/v1", timeout=30) as client:
    item = client.get_json("/items/42/")
```

```python
from api_explorer.testing import next_link_api

api = next_link_api(250, page_size=100)  # 250 records, 100 a page
client = api.client()  # a Client wired to the fake

records = list(client.records("/items/"))
assert records == list(range(250))
assert api.queries == [{}, {"page": "2"}, {"page": "3"}]
```

`records()` follows the pages and yields each record. Its main arguments:

- `results_key`: where the records are in each page, a path with fallbacks such as `"results|data|items"`; `None` when the body itself is the list.
- `count_key`: the total count, if the API gives one, for progress and an exact end.
- `paging`: how to reach the next page (see [paging](paging.md)); next-page links by default.
- `method` and `json`: for a search sent as a POST body. A POST isn't retried unless you pass `retry=True`, since repeating it could repeat a change.
- `model`: turns each record into a typed object, a pydantic model, msgspec Struct, dataclass or function.
- `max_pages`: stop early, to look at the first few pages.
- `incremental`: fetch only what changed (see [incremental](incremental.md)).

A missing results path is an error, not an empty page, so a mistyped key can't pass for "no data":

```python
from api_explorer import ApiError

try:
    list(client.records("/items/", results_key="data"))
except ApiError as error:
    assert "No 'data'" in str(error)
```

### Errors reported inside a 200

Some APIs answer 200 and put the error in the body. Without a check, such a page can read as the last, empty page, and a run ends early with no warning. A response check turns it into an `ApiError`; with `temporary=True`, the retry policy tries again first:

```python
from api_explorer import error_at, http
from api_explorer.testing import replies

api = replies(http.Response(200, json={"error": "quota exceeded"}))
checked = api.client(check=error_at("error|errors"))
try:
    checked.get_json("/items/")
except ApiError as error:
    assert "quota exceeded" in str(error)
```

`error_at` lets through `null`, `false`, `0` and empty values, which many APIs send on success.

### How requests are sent

A transport sends each request: everything the client sends goes through it, token requests and retries included.

| You want... | Transport |
|-------------|-----------|
| the default: fast, mature, installed almost everywhere | `Urllib3Transport()` |
| proxies, certificates or connection limits | `Urllib3Transport(pool=urllib3.ProxyManager(...))` |
| HTTP/2, or an `httpx2.Client` and its auth | `Httpx2Transport(http=httpx2.Client(http2=True))` (the `httpx2` extra) |
| another library | a transport of your own: `send(request, timeout)` and `close()` ([build-your-own](build-your-own.md)) |

```python skip
import httpx2
from api_explorer import Httpx2Transport

transport = Httpx2Transport(http=httpx2.Client(http2=True, proxy="http://proxy:8080"))
client = Client("https://api.example.com", transport=transport)
```

A transport you pass stays open when the client closes, so several clients can share one; close it yourself. On sequential JSON pages, `Urllib3Transport` sends about 1.4 times as many small pages a second as `Httpx2Transport`, and 2.5 times as many large ones; against an API's own response time, either is a small part of a run.

## Traps

- Endpoints are joined to the base URL: `"/items/"` and `"items/"` both work. An absolute URL is used as given.
- `max_pages` and `incremental` can't be used together: a run cut short looks complete to a tracker.
- Keep secrets out of URLs (`?api_key=`): they are hidden in errors and logs, but prefer a header ([auth](auth.md)).
