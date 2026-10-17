# api-explorer

Pull data from paginated JSON APIs, and shape it for whichever dataframe library you use.

- **Paging:** follows next-page links in the body or the `Link` header, sends back cursors, or steps `limit` and `offset` itself, for GET or POST, and knows when it has reached the end.
- **Reliability:** retries network errors, timeouts, 429 and 5xx responses, and renews short-lived access tokens.
- **Output:** turns nested JSON into tidy rows, then into a polars, pandas or pyarrow frame.
- **Explorer:** a marimo notebook to explore an unfamiliar API, then copy the fetch as a script.

## Install

```bash
uv add api-explorer            # or: pip install api-explorer
uv add "api-explorer[fast]"    # with an extra
```

The core needs only `httpx2` and `narwhals`. Optional extras add more:

| Extra | Adds |
|-------|------|
| `fast` | faster JSON parsing with msgspec, giving the same results |
| `scrape` | HTML scraping with `api_explorer.scrape` (parsel) |
| `explorer` | the explorer notebook (marimo) |

## Quick start

```python
import os

from api_explorer import Client, Offset, select, to_frame, token_headers

with Client(
    "https://host/api", headers=token_headers(os.environ["API_TOKEN"])
) as client:
    records = list(client.records("/items/", count_key="count"))

df = to_frame(select(records, ["id", "name", "owner.email"]), "polars")
```

For a Django Ninja style API (no next link, a `count`, and routes without a trailing slash):

```python
with Client("https://host/api", trailing_slash=False) as client:
    records = list(
        client.records(
            "/items", results_key="items", count_key="count", paging=Offset(limit=100)
        )
    )
```

Every public function and class has a docstring that explains its rules.

## Light by default, richer with what you have

Each piece works with no extra packages, and uses better ones when you have them:

| Piece | Default | With other packages |
|-------|---------|---------------------|
| JSON parsing | the standard library | msgspec, used by itself when installed (same results, faster); orjson by name, with `json_decoder("orjson")` |
| Typed records | plain dicts | `records(..., model=Item)` with a pydantic model, msgspec Struct or dataclass |
| Dataframes | named by you | `to_frame(records)` uses the library you've already imported, else the first one installed |
| Retries | 3 tries, idempotent methods, temporary errors | `Retry(attempts=5, statuses=..., methods=...)`, or your own policy: any object with `wait(failure)` |
| HTTP | an httpx2 client made for you | `Client(http=httpx2.Client(...))` for proxies, HTTP/2, certificates or event hooks |
| Logging | silent | `enable_logging()`; every line is an event with data (`record.event`, `record.data`) |
| Paging | next links (body or `Link` header), cursors, limit/offset, GET or POST | your own `Paging`: it sees each page's body and response; `with_query` sets a query parameter |
| Auth | fixed tokens; access and refresh tokens, renewed safely across threads | log in with `PasswordLogin`, OAuth2 `ClientCredentials`, or your own `Login`; any `httpx2.Auth` |

A search API that pages with POST, with the query and the cursor in the body:

```python
from api_explorer import Cursor

records = client.records(
    "/search/",
    method="POST",
    json={"query": "late orders"},
    results_key="hits",
    paging=Cursor(send_in="body"),
    retry=True,  # safe here: this POST only reads
)
```

The interfaces are Protocols: your pieces match their shape and never inherit from ours, and a type checker confirms they fit. `Paging`, `RetryPolicy`, `Login` and `ResolvesUrls` (an auth with URLs of its own) are the main ones; `check_paging` and `check_retry_policy` check behaviour the types can't.

`Client.records` is built from public parts, `paginate`, a `Paging`, `read_page` and `converter`, so a loop of your own can reuse the same rules. `paginate` pages by whatever your fetch needs, not only URLs, with the same loop protection:

```python
from api_explorer import paginate, read_page


def fetch(body):
    return client.json_body(client.request("POST", "/search/", json=body))


def next_request(page, body):
    return {**body, "cursor": page["cursor"]} if page.get("cursor") else None


for page in paginate(fetch, {"query": "late orders"}, next_request):
    rows, _ = read_page(page, "hits")
```

```python
from pydantic import BaseModel

from api_explorer import Client, Retry, to_frame


class Item(BaseModel):
    id: int
    name: str


with Client("https://host/api", retry=Retry(attempts=5)) as client:
    # A record that doesn't fit Item raises ApiError, naming the record.
    items = list(client.records("/items/", model=Item))

# In whichever dataframe library this session already uses.
df = to_frame(items)
```

## Logging

The package prints nothing until you switch logging on:

```python
from api_explorer import enable_logging, logging_to

enable_logging()  # INFO and up on stderr: a line per page, and retries
enable_logging("DEBUG", http=True)  # every decision, and each request

with logging_to(my_handler, "INFO"):  # for a while, to a handler of yours
    ...
```

Each line the package logs is an event: `record.event` is a short name (`page`, `done`, `retry`, `stop`, `renew`, and so on) and `record.data` holds its numbers, so a progress bar or a log shipper can act on events without reading text. The full list is in the `api_explorer.logs` docstring.

`Client(name="crm")` labels every line from that client's requests, `[crm]` in the readable format and `record.client` for handlers. That includes lines your own pieces log during a request: log under your own logger, add it with `enable_logging(loggers=("mypkg",))`, and use `log_event` for the same `event` and `data` fields.

## Testing your code

`api_explorer.testing` has fake APIs that page, fail and authenticate like real ones, in memory. Use them to test code that fetches from APIs, with any test framework and without the network. (respx and pytest-httpx are built for httpx, not httpx2, so they don't see these requests.)

```python
from api_explorer import Offset
from api_explorer.testing import flaky, offset_api


def test_my_pipeline_survives_a_capped_server_and_a_hiccup():
    api = flaky(offset_api(250, cap=100), 503)  # one 503, then pages of at most 100
    records = list(
        api.client().records("/items/", results_key="items", paging=Offset(limit=1000))
    )
    assert records == list(range(250))
    assert len(api.calls) == 4  # the 503, then 3 pages
```

| Fake | Behaves like |
|------|--------------|
| `next_link_api` | Django REST Framework: `count`, `next`, `results` |
| `offset_api` | Django Ninja: `?limit=&offset=`, with an optional server cap and count |
| `cursor_api` | a cursor in the query string or a POST body |
| `link_header_api` | GitHub: a bare list, with a `Link` header to the next page |
| `TokenServer` | simplejwt: login, refresh, rotation and expiry |
| `json_pages`, `replies` | fixed pages by URL, or fixed responses in turn |
| `flaky(api, 503, ...)` | any of these, failing first as told |

Each returns a `FakeApi`, which remembers every request (`.calls`, `.paths`, `.queries`, `.bodies`). `.client()` gives a `Client` wired to it, whose retries don't wait.

### Testing your own pieces

Pieces you build on the package's interfaces test the same way: point them at a fake, and check what happened. Two checks cover the promises every retry policy and paging style must keep.

```python
import httpx2

from api_explorer import Client, PageRequest, read_page
from api_explorer.testing import check_paging, check_retry_policy, fetched, replies


def test_my_paging_style():
    # On its own: fetched() builds the page a style receives.
    first, next_link = MyStyle().begin(PageRequest("http://api.test/api/items/"), read)
    assert next_link(fetched({"links": {"next": "?page=2"}}), first) == "?page=2"
    # End to end: every record, once each, and it stops.
    check_paging(MyStyle(), my_fake_api, expected_records)


def test_my_retry_policy():
    check_retry_policy(MyPolicy())  # always gives up; no negative or endless waits
    api = replies(503, 200)
    api.client(retry=MyPolicy()).request("GET", "/items/")
    assert len(api.calls) == 2


def test_my_auth_signs_each_request():
    api = replies(200)
    api.client(auth=MyAuth()).request("GET", "/items/")
    assert "X-Signature" in api.calls[0].headers


def test_with_my_own_httpx2_client():
    api = replies(httpx2.Response(200, json={"results": [1], "next": None}))
    http = httpx2.Client(
        base_url="http://api.test/api", transport=httpx2.MockTransport(api)
    )
    assert list(Client(http=http).records("/items/")) == [1]


def read(page):
    return read_page(page.body)
```

## The explorer

```bash
uvx marimo edit --sandbox notebooks/explorer.py
```

The notebook's header installs the package from this repo. To use the notebook on its own, outside a clone of the repo, delete its `[tool.uv.sources]` lines, and it installs `api-explorer` from PyPI instead.

Fill in the API, press **Fetch**, and pick the columns you want. The **Code** tab then shows a script that repeats the fetch; `uv run` installs what it needs. Secrets come from `$API_TOKEN`, `$API_REFRESH_TOKEN`, `$API_USERNAME` and `$API_PASSWORD`, and never appear in the script.

## How it is built

The package is a set of layers, each built only on the ones before it: `errors` and `paths`, then `records`, `frames` and `paging`, then `auth` and `client`, then `scrape` and `plan`. See [AGENTS.md](AGENTS.md), section 12.

## Development

```bash
uv sync
uv run pytest      # every test: this is the project's test command
uvx ruff check && uvx ruff format --check
```

Changes start as an issue. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT. See [LICENSE](LICENSE).
