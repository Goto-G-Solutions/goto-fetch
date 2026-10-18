---
name: api-explorer
description: Use when writing Python that pulls data from a paginated JSON (REST) API, fetches only what changed since the last run, or stores API data in DuckDB or SQLite, with the api_explorer package. Covers paging styles, auth and token renewal, retries, incremental fetching, warehouses, testing against fake APIs, and building custom pieces on its interfaces.
---

# api-explorer

`api_explorer` already handles the hard parts of pulling API data: paging to the end, retries, token renewal, secrets kept out of logs, incremental runs that never skip a record, and all-or-nothing storage. Compose its pieces; don't rewrite them.

## How to work

1. Read the guide for the piece you need: `python -m api_explorer.docs <page>`. The index is `python -m api_explorer.docs`. Pages: client, paging, auth, retries, incremental, storage, records-and-frames, fetch-plan, logging, testing, scraping, build-your-own.
2. Start from a `Client`, page with `client.records(...)` and a paging style, and keep results with a warehouse and a tracker when runs repeat.
3. When no built-in piece fits, implement the interface ([build-your-own]) and run its `check_*` from `api_explorer.testing`.
4. Test with the fake APIs in `api_explorer.testing`; never call a real API in tests.

## The usual shape

```python
from api_explorer import Client, Incremental, NextLink, SqliteWarehouse, tracked_run
from api_explorer.testing import changes_api

client = changes_api([{"id": 1, "updated_at": "2026-01-01T00:00:00Z"}]).client()
shop = SqliteWarehouse("shop.db")
with tracked_run(shop, "orders", Incremental, "updated_at", "updated_since") as since:
    stored = shop.write(
        "orders",
        client.records("/orders/", paging=NextLink(), incremental=since),
        key="id",
    )
assert stored == 1
```

With a real API: `Client("https://api.example.com", headers=token_headers(os.environ["API_TOKEN"], "Bearer"))`.

## Rules that keep data safe

- Give a tracked run every page: never `max_pages` with `incremental`.
- Store with `key=` when records can change (`updated_at`), so they replace their rows.
- Prefer a cursor, `Keyset` or `Changes` over page numbers when the data changes during a run.
- A POST that only reads needs `retry=True`; other POSTs are never retried.
- For a small or busy server, keep to a rate: `Client(transport=Limited(Urllib3Transport(), RateLimit(2)))`.
- Keep secrets in environment variables and headers, never in URLs or code.

[build-your-own]: python -m api_explorer.docs build-your-own
