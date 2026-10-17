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
| Progress | one log line per page | each line also carries the numbers (`record.progress`), for a progress bar |
| Paging and auth | next links (body or `Link` header), cursors, limit/offset, GET or POST; tokens | your own `Paging` (it sees each page's body and response) or `httpx2.Auth` |

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
