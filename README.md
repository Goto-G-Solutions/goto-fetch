# api-explorer

Pull data from paginated JSON APIs, and shape it for whichever dataframe library you use.

- **Paging:** follows next-page links, or steps `limit` and `offset` itself, and knows when it has reached the end.
- **Reliability:** retries network errors, timeouts, 429 and 5xx responses, and renews short-lived access tokens.
- **Output:** turns nested JSON into tidy rows, then into a polars, pandas or pyarrow frame.
- **Explorer:** a marimo notebook to explore an unfamiliar API, then copy the fetch as a script.

## Install

```bash
uv add "api-explorer @ git+https://github.com/OWNER/api-explorer"
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
| Retries | 3 tries, idempotent methods, temporary errors | `Client(retry=Retry(attempts=5, statuses=..., methods=...))` |
| Paging and auth | next links, limit/offset, tokens | your own `Paging` or `httpx2.Auth` |

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
