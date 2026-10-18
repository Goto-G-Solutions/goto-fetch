# goto-fetch

Pull data from paginated JSON APIs safely, fetch only what changed, and store it for analysis.

- **Paging:** follows next-page links in the body or the `Link` header, sends back cursors, steps `limit` and `offset` itself, or pages by a sorted field (keyset), for GET or POST, and knows when it has reached the end.
- **Reliability:** retries network errors, timeouts, 429 and 5xx responses, renews short-lived access tokens, and catches errors an API reports inside a 200.
- **Incremental:** fetches only what changed since the last run, by a field such as `updated_at` or by a hash of each record, and notices deleted records, without losing any when a run fails.
- **Storage:** stores runs in DuckDB or SQLite, all or nothing, with the columns following the data and no value lost; or a database of your own.
- **Output:** turns nested JSON into tidy rows, then into a polars, pandas or pyarrow frame.
- **Explorer:** a marimo notebook to explore an unfamiliar API, then copy the fetch, or the whole stored and tracked run, as a script.

## Who it is for

`goto-fetch` is a library to embed, not a platform to adopt: a fast start for a pipeline of your own that you can read end to end. The built-in pieces cover what most pipelines share (paging to the end, retries, tokens that renew, incremental runs that never skip a record, storage that is all or nothing), each sits behind a small interface, and you bring your own for the parts you care about: a transport, a paging style, a retry policy, a tracker, a warehouse on your own database. Records are plain dicts, so what comes out works with whatever you use next, and every piece can be tested against the fake APIs that ship with it.

It is a fit for one team talking to a few APIs. For dozens of sources, a catalogue of ready-made connectors, nested data unnested into child tables, or parallel loads into many destinations, an ELT platform such as dlt, Airbyte or Meltano fits better: this package is synchronous, fetches one stream at a time, and stores to DuckDB or SQLite out of the box.

## Guides

Guides for each piece ship with the package, for people and coding agents: `python -m goto_fetch.docs` lists them, and `python -m goto_fetch.docs paging` prints one. In the repository they are in [src/goto_fetch/docs](src/goto_fetch/docs/README.md), indexed in [llms.txt](llms.txt). To give a coding agent this package's skill in your own project:

```bash
mkdir -p .claude/skills/goto-fetch
python -m goto_fetch.docs skill > .claude/skills/goto-fetch/SKILL.md
```

## Install

```bash
uv add goto-fetch                      # or: pip install goto-fetch
uv add "goto-fetch[fast,polars,duckdb]"  # with extras, any mix of them
```

The core needs only `urllib3` and `narwhals`. Optional extras add more, and combine:

| Extra | Adds |
|-------|------|
| `fast` | faster JSON parsing with msgspec, giving the same results |
| `polars`, `pandas` | a dataframe library for `to_frame` (pyarrow works too, by name, without an extra) |
| `duckdb` | storing records and run state in a DuckDB file with `DuckDBWarehouse` |
| `scrape` | HTML scraping with `goto_fetch.scrape` (parsel) |
| `httpx2` | sending with httpx2 (`Httpx2Transport`): HTTP/2, an `httpx2.Client` of your own |
| `explorer` | marimo, to run the explorer notebook from a clone of this repository |

## Quick start

```python
import os

from goto_fetch import Client, Offset, select, to_frame, token_headers

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
| HTTP | `Urllib3Transport`, fast and installed almost everywhere | `Httpx2Transport` (the `httpx2` extra) for HTTP/2 or an `httpx2.Client` of your own; `Client(rate_limit=RateLimit(2))` to keep to a rate; any `Transport` of your own |
| Logging | silent | `enable_logging()`; every line is an event with data (`record.event`, `record.data`) |
| Paging | next links (body or `Link` header), cursors, limit/offset, keyset, GET or POST | your own `Paging`: it sees each page's body and response; `with_param` sets a query or body parameter |
| Incremental fetching | everything, every run | `Incremental(field, param)` asks only for changes; `Changes(key)` passes on only changed records and lists deleted ones |
| Errors in a 200 | trusted as data | `Client(check=error_at("error\|errors"))`, or your own check: a function from response to problem |
| Auth | fixed tokens; access and refresh tokens, renewed safely across threads | log in with `PasswordLogin`, OAuth2 `ClientCredentials`, or your own `Login`; an `Auth` of your own (`auth_flow`), or an `httpx2.Auth` on `Httpx2Transport`'s client |

A search API that pages with POST, with the query and the cursor in the body:

```python
from goto_fetch import Cursor

records = client.records(
    "/search/",
    method="POST",
    json={"query": "late orders"},
    results_key="hits",
    paging=Cursor(send_in="body"),
    retry=True,  # safe here: this POST only reads
)
```

Some APIs answer 200 and put the error in the body. Left alone, an error page that still holds an empty list of results reads as the last page, and the run ends with part of the data and no warning. A response check turns such a page into an `ApiError`; with `temporary=True`, the retry policy tries again first, as for a 503:

```python
from goto_fetch import Client, error_at

client = Client(
    "https://api.example.com", check=error_at("error|errors", temporary=True)
)
```

To fetch only what changed since the last run, give `records` an `Incremental`: the field that grows when a record is added or changed, and the parameter the API filters by. Field and parameter names are settings, since APIs differ (`created_at` or `updated_at`; `updated_since`, `updated_at__gte` or a key in a POST body). `tracked_run` keeps its state in an SQLite file (Python's own `sqlite3`, so no extra package), and saves it only if the whole block worked, your storing included:

```python
from goto_fetch import Incremental, SqliteState, tracked_run

state = SqliteState("state.db")
with tracked_run(state, "orders", Incremental, "updated_at", "updated_since") as since:
    store(client.records("/orders/", incremental=since))
```

Two rules keep it from losing data. The state moves on only when a run has read every page, since records may come in any order and a failed run could have skipped older ones. And it asks from the last value itself, not after it, then passes over what it has already seen there, so records that share the last value aren't lost. `created_at` finds new records only; `updated_at` also finds edits, so a record can come back with new values, and storing must replace it by its id rather than add it again.

One trap: paging by page number or offset can skip a record when the data changes during a run, because the records after a changed one shift a place. With `Incremental`, a skipped record is lost for good, since later runs start past it. Use a paging style that doesn't shift: a server cursor (`Cursor`), or, when the API can sort by the field, `Keyset`, which asks each page from the last value of the one before. `Changes` finds such a record again on its next run, so it suits APIs that offer neither.

```python
from goto_fetch import Keyset

with tracked_run(state, "orders", Incremental, "updated_at", "updated_since") as since:
    store(
        client.records(
            "/orders/?ordering=updated_at",
            incremental=since,
            paging=Keyset("updated_at", "updated_since", limit=500),
        )
    )
```

Keyset pages overlap at each page's last value, and `Incremental` passes the repeats over. When more records share one value than a page holds, Keyset stops with an error rather than skip any; raise `limit`.

When an API has no such field, or doesn't always update it, `Changes` fetches everything and passes on only new and changed records, by a fingerprint (a hash) of each. It also lists the records that disappeared, which a timestamp never shows. Leave out fields that change on every fetch, or every record looks changed. For an API that gives each record a version or ETag, `fingerprint=lambda r: str(r["version"])` replaces the hash:

```python
from goto_fetch import Changes

with tracked_run(
    state, "products", Changes, key="id", ignore=["fetched_at"]
) as changes:
    store(client.records("/products/", incremental=changes))
remove(changes.deleted)  # ids gone since the last run
```

The state can live anywhere with `get(name)` and `put(name, value)`; `goto_fetch.testing.check_state_store` checks such a store, and `MemoryState` keeps it in memory for tests.

To keep the records too, store them in a warehouse. A `DuckDBWarehouse` (the `duckdb` extra) or an `SqliteWarehouse` (nothing to install) is one file that holds both the tables and the state:

```python
from goto_fetch import DuckDBWarehouse

shop = DuckDBWarehouse("shop.duckdb")  # or SqliteWarehouse("shop.db")
with tracked_run(shop, "products", Changes, key="id") as changes:
    records = client.records("/products/", incremental=changes)
    shop.write("products", records, key="id", deleted_by=changes)
rows = shop.read("products", limit=10)
```

Every warehouse keeps the same promises, and these are what make tracked runs safe:

- one `write` is all or nothing: a failed request or a failed write leaves the table as it was;
- with a `key`, a record replaces the row with its key (what `updated_at` runs need), the last of a key winning;
- `deleted_by` removes the rows a tracker lists as gone, only once the records are stored;
- no value is lost: `read` gives each value back as written, or as its JSON text where its column had to change type.

The two database warehouses first copy a run to temporary files, so a failed request never touches the database, then make one transaction of it. Columns follow the records: nested objects and lists become JSON columns (`customer->>'name'` in DuckDB's SQL), text stays text, timestamps included, new fields add columns, and a column whose values change type is widened (integers beside floats to floats when a float holds every one exactly, anything else to text, with a warning). Read the file with its own tools too, such as `duckdb.connect("shop.duckdb").sql("SELECT * FROM products").pl()`.

Any object with `write` and `read` that keeps those promises is a `Warehouse`, and works with `FetchPlan.store(client, warehouse=...)`; `goto_fetch.testing.check_warehouse` checks one, and `MemoryWarehouse` stands in for tests. For another SQL database, implement `SqlTables` (open a transaction, describe a table, alter it, load a file, delete keys) and call `write_run`, which keeps the promises; the DuckDB and SQLite warehouses are built that way.

`FetchPlan` describes one fetch as data (the API, its auth, paging, tracking and where to store), which is what the explorer builds. `plan.records(client)` runs it, `plan.store(client)` runs it into its warehouse, and `plan.script()` writes a uv script that does the same, from the same description, with secrets read from environment variables.

The interfaces are Protocols: your pieces match their shape and never inherit from ours, and a type checker confirms they fit. `Paging`, `RetryPolicy`, `Login`, `ResolvesUrls` (an auth with URLs of its own), `Tracker` (what a run delivers: `apply`, `track`, `state`), `ReportsDeletions` (a tracker that knows what is gone), `StateStore` and `Warehouse` are the main ones; one-function seams are plain function types (`Decoder`, `ResponseCheck`, `Fingerprint`). `check_paging`, `check_retry_policy`, `check_tracker`, `check_state_store`, `check_warehouse` and `check_transport` check behaviour the types can't.

`Client.records` is built from public parts, `paginate`, a `Paging`, `read_page` and `goto_fetch.models.converter`, so a loop of your own can reuse the same rules. `paginate` pages by whatever your fetch needs, not only URLs, with the same loop protection:

```python
from goto_fetch import paginate, read_page


def fetch(body):
    return client.json_body(client.request("POST", "/search/", json=body))


def next_request(page, body):
    return {**body, "cursor": page["cursor"]} if page.get("cursor") else None


for page in paginate(fetch, {"query": "late orders"}, next_request):
    rows, _ = read_page(page, "hits")
```

```python
from pydantic import BaseModel

from goto_fetch import Client, Retry, to_frame


class Item(BaseModel):
    id: int
    name: str


with Client("https://host/api", retry=Retry(attempts=5)) as client:
    # A record that doesn't fit Item raises ApiError, naming its position.
    items = list(client.records("/items/", model=Item))

# In whichever dataframe library this session already uses.
df = to_frame(items)
```

## Scraping

For pages without an API, `goto_fetch.scrape` (the `scrape` extra) follows "next" links through HTML with the same paging loop and retries:

```python
from goto_fetch.scrape import CSSField, ScraperConfig, scrape

config = ScraperConfig(
    item_selector="article.product",
    fields={"name": CSSField("h2"), "price": CSSField(".price")},
    next_page_selector="a.next",  # its href leads to the next page
)
rows = list(scrape(client, "/shop/", config))
```

## Logging

The package prints nothing until you switch logging on:

```python
from goto_fetch import enable_logging, logging_to

enable_logging()  # INFO and up on stderr: a line per page, and retries
enable_logging("DEBUG", http=True)  # every decision, and each request

with logging_to(my_handler, "INFO"):  # for a while, to a handler of yours
    ...
```

Each line the package logs is an event: `record.event` is a short name (`page`, `done`, `retry`, `stop`, `renew`, and so on) and `record.data` holds its numbers, so a progress bar or a log shipper can act on events without reading text. The full list is in the `goto_fetch.logs` docstring.

Secrets in URLs, such as `?api_key=...` or `?access_token=...`, show as `***` in errors and in the lines `enable_logging` prints, including each request's.

`Client(name="crm")` labels every line from that client's requests, `[crm]` in the readable format and `record.client` for handlers. That includes lines your own pieces log during a request: log under your own logger, add it with `enable_logging(loggers=("mypkg",))`, and use `goto_fetch.logs.log_event` for the same `event` and `data` fields.

## Testing your code

`goto_fetch.testing` has fake APIs that page, fail and authenticate like real ones, in memory. Use them to test code that fetches from APIs, with any test framework and without the network. Each fake is a `Transport`, so it goes straight into `Client(transport=...)`; mocking libraries built for httpx or requests don't see these requests.

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
```

| Fake | Behaves like |
|------|--------------|
| `next_link_api` | Django REST Framework: `count`, `next`, `results` |
| `offset_api` | Django Ninja: `?limit=&offset=`, with an optional server cap and count |
| `cursor_api` | a cursor in the query string or a POST body |
| `link_header_api` | GitHub: a bare list, with a `Link` header to the next page |
| `changes_api` | filters by a field, `?updated_since=`, for testing `Incremental` |
| `keyset_api` | sorted by a field, paged from a value with `?limit=`, for testing `Keyset` |
| `TokenServer` | simplejwt: login, refresh, rotation and expiry, and the OAuth2 client credentials grant |
| `json_pages`, `replies` | fixed pages by URL, or fixed responses in turn |
| `flaky(api, 503, ...)` | any of these, failing first as told |

Each returns a `FakeApi`, which remembers every request (`.calls`, `.paths`, `.queries`, `.bodies`). `.client()` gives a `Client` wired to it, whose retries don't wait.

### Testing your own pieces

Pieces you build on the package's interfaces test the same way: point them at a fake, and check what happened. Six checks cover the promises every retry policy, paging style, tracker, state store, warehouse and transport must keep.

```python
from goto_fetch import Client, PageRequest, http, read_page
from goto_fetch.testing import (
    MemoryState,
    check_paging,
    check_retry_policy,
    check_state_store,
    check_tracker,
    check_transport,
    fetched,
    replies,
)


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


def test_my_tracker():
    # make(state) builds a run's tracker from the last run's state. The
    # check: plain JSON state that moves on only after a complete run, a
    # first run that delivers everything, and a next run that delivers
    # nothing when nothing changed.
    check_tracker(lambda state: MyTracker(state=state), sample_records)


def test_my_state_store():
    check_state_store(MyStore)  # get gives copies, put replaces, names apart


def test_my_auth_signs_each_request():
    api = replies(200)
    api.client(auth=MyAuth()).request("GET", "/items/")
    assert "X-Signature" in api.calls[0].headers


def test_my_transport():
    check_transport(MyTransport)  # over real HTTP on this machine, against a fake


def test_a_client_set_up_elsewhere_against_a_fake():
    api = replies(http.Response(200, json={"results": [1], "next": None}))
    client = Client("http://api.test/api", transport=api)  # a fake is a transport
    assert list(client.records("/items/")) == [1]


def read(page):
    return read_page(page.body)
```

## The explorer

```bash
uvx marimo edit --sandbox notebooks/explorer.py
```

The notebook's header installs the package from this repo. To use the notebook on its own, outside a clone of the repo, delete its `[tool.uv.sources]` lines, and it installs `goto-fetch` from PyPI instead.

Fill in the API, press **Fetch**, and pick the columns you want. The **Code** tab then shows a script that repeats the fetch; `uv run` installs what it needs. Secrets come from `$API_TOKEN`, `$API_REFRESH_TOKEN`, `$API_ACCESS_TOKEN`, `$API_USERNAME`, `$API_PASSWORD` and `$API_CLIENT_SECRET`, and never appear in the script.

The **Store** section saves the records in a DuckDB or SQLite file instead, and can fetch only what changed since the last run, by a field such as `updated_at` or by a hash of each record. Where each run stopped is kept in the same file. The script then does the same, so a scheduled `uv run fetch.py` keeps the table up to date. A tracked fetch reads every page, so set **Pages** to 0.

## How it is built

The package is a set of layers, each built only on the ones before it: the primitives (`errors`, `paths`, `decode`, `models`, `calls`, and `http`, the HTTP vocabulary), then logging, records, retries and checks, then `ordering`, `frames`, `auth`, the two transports and `limits`, then `paging`, then the trackers (`incremental`), then `client`, `state` and the `warehouse` interface, then the built-in warehouses, `scrape` and `testing`, and last `plan`. See [AGENTS.md](AGENTS.md), section 12.

## Development

```bash
uv sync
uv run pytest      # every test: this is the project's test command
uvx ruff check && uvx ruff format --check
```

Changes start as an issue. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT. See [LICENSE](LICENSE).
