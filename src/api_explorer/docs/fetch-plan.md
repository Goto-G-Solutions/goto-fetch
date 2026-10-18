# FetchPlan and the explorer

A `FetchPlan` describes one fetch as data: the API, its auth, paging, tracking and where to store. It is what the explorer notebook builds, and it can run the fetch, store it, or write a script that does the same.

## When to use it

- To keep a fetch as configuration rather than code, or to hand someone a script.
- From the explorer: fill in the panel, press **Fetch**, and the **Code** tab shows the plan's script.

## What it handles for you

- **One description:** each piece is described once, as a `Call`, and both run and written into the script from it, so a script can't drift from what the plan runs.
- **Secrets** are used to run but never written: scripts read `$API_TOKEN`, `$API_REFRESH_TOKEN`, `$API_ACCESS_TOKEN` (if set), `$API_USERNAME`, `$API_PASSWORD` and `$API_CLIENT_SECRET`. App settings such as a client ID are written as given.
- **Plans that could lose records are refused:** tracking without a table, tracking with `max_pages`, `Changes` without a key, keyset paging without its filter; `store()` and `script()` refuse a tracked plan with nowhere to store.

## Examples

```python
from api_explorer import http
from api_explorer import FetchPlan
from api_explorer.testing import changes_api

plan = FetchPlan(
    "http://api.test/api",
    "/orders/",
    track="incremental",
    change_field="updated_at",
    change_param="updated_since",
    warehouse="shop.db",
    warehouse_kind="sqlite",
    table="orders",
    key="id",
)
api = changes_api([{"id": 1, "updated_at": "2026-01-01T00:00:00Z"}])
with plan.client(transport=api) as client:
    assert plan.store(client) == 1  # tracked: the next run fetches only changes

script = plan.script()  # a uv script that does the same
assert "SqliteWarehouse('shop.db')" in script
assert "tracked_run(" in script
```

`rate_limit=2` keeps every request the plan sends to two a second, in its script too. Without a warehouse, `plan.records(client)` yields the records, and `plan.script(columns=[...], backend="polars")` writes a script that selects columns and builds a frame. `plan.store(client, warehouse=...)` stores into any `Warehouse` of your own.

The script's header installs this package the way it is installed here (`package_requirement()`), with the `duckdb` extra when needed:

```python skip
print(plan.script(requirement=package_requirement()))
# then: uv run fetch.py
```

## The explorer

```bash
uvx marimo edit --sandbox notebooks/explorer.py
```

Its fields fill from `$API_BASE_URL` and the secret variables above. The **Store** section picks a DuckDB or SQLite file, a table, a key, and what each run fetches; a tracked fetch reads every page, so set **Pages** to 0.

## Traps

- URLs are written into scripts as given: keep secrets out of them and use header auth.
- `store()` keeps a tracked plan's state under the table's name, in the warehouse unless you pass `state=`.
