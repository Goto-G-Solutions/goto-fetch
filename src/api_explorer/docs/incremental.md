# Incremental fetching

A tracker makes a run deliver only what changed since the last one. Pass it to `client.records(incremental=...)`, and keep its state between runs with `tracked_run`.

## Choosing a tracker

| The API... | Use | Saves |
|------------|-----|-------|
| has a field that grows on every change (`updated_at`) and can filter by it | `Incremental(field, param)` | network: only changes are fetched |
| has such a field, but can't filter | `Incremental(field)` | storing: every page is fetched, only changes come through |
| has no such field, or doesn't always update it | `Changes(key)` | storing: everything is fetched, changes and deletions come through |

`created_at` finds new records only; `updated_at` also finds edits, so a record can come back, and storing must replace it by key (see [storage](storage.md)).

## What it handles for you

- **No lost records when a run fails.** A tracker's state moves on only when a run has read every page, and `tracked_run` saves it only when the whole block worked, your storing included. A failed run repeats records next time; it never skips them.
- **Ties.** `Incremental` asks from the last value itself (`>=`) and passes over what it saw there, so records sharing the last value aren't lost.
- **Odd values.** ISO timestamps compare as times (`Z` and `+00:00` agree), numbers as numbers; values that can't be compared stop the run rather than guess.
- **Deletions.** `Changes` lists the keys gone since the last run (`deleted`), and a warehouse removes them (`deleted_by=`).

## Examples

```python
from api_explorer import Changes, Incremental, SqliteWarehouse, tracked_run
from api_explorer.testing import changes_api

data = [
    {"id": 1, "updated_at": "2026-01-01T00:00:00Z"},
    {"id": 2, "updated_at": "2026-01-02T00:00:00Z"},
]
api = changes_api(data)  # filters by ?updated_since=
shop = SqliteWarehouse("shop.db")  # keeps the records and where each run stopped


def run_once():
    with tracked_run(
        shop, "orders", Incremental, "updated_at", "updated_since"
    ) as since:
        return shop.write(
            "orders", api.client().records("/orders/", incremental=since), key="id"
        )


assert run_once() == 2
data.append({"id": 3, "updated_at": "2026-01-03T00:00:00Z"})
assert run_once() == 1  # only the new record
assert api.queries[-1] == {"updated_since": "2026-01-02T00:00:00Z"}
```

`Incremental(start=...)` says where a first run begins when there is no state; `send_in="body"` puts the filter in a POST body; `send_as=` changes the form of the value sent, such as a date for a time.

`Changes` fetches everything and compares a fingerprint of each record:

```python
from api_explorer.testing import next_link_api

products = [{"id": i, "price": 10, "fetched_at": "now"} for i in range(3)]


def sync():
    api = next_link_api(products)
    with tracked_run(
        shop, "products", Changes, key="id", ignore=["fetched_at"]
    ) as changes:
        stored = shop.write(
            "products",
            api.client().records("/products/", incremental=changes),
            key="id",
            deleted_by=changes,
        )
    return stored, changes.deleted


assert sync() == (3, [])
products[1] = {"id": 1, "price": 12, "fetched_at": "later"}
del products[2]
assert sync() == (1, [2])  # one change, one deletion
```

`ignore` leaves out fields that change on every fetch. For an API that gives a version or ETag, `fingerprint=lambda record: str(record["version"])` replaces the hash.

### Keyset paging

For an API sorted by the tracked field, `Keyset` asks each page from the last value of the one before. Unlike page numbers, it can't skip a record edited during the run. Pair it with a tracker, which passes over the repeats where pages overlap:

```python
from api_explorer import Keyset
from api_explorer.testing import keyset_api

rows = [{"id": i, "updated_at": f"2026-01-01T00:0{i}:00Z"} for i in range(6)]
since = Incremental("updated_at", "updated_since")
records = (
    keyset_api(rows)
    .client()
    .records(
        "/orders/",  # with a real API, ask for the order too: ?ordering=updated_at
        incremental=since,
        paging=Keyset("updated_at", "updated_since", limit=4),
    )
)
assert [r["id"] for r in records] == [0, 1, 2, 3, 4, 5]
```

`Keyset` refuses unsorted pages, an API that ignores the filter, and more records at one value than a page holds (raise `limit`).

## Traps

- **Page numbers and offsets with `Incremental`** can lose a record for good when the data changes during a run, since the rest shift a place. Use a cursor, `Keyset`, or `Changes`, which finds such a record on its next run.
- **`Changes.deleted` needs all-or-nothing storing.** The built-in warehouses are; a store that writes as records arrive can keep a row whose record was deleted.
- **`max_pages` with a tracker is refused:** a run cut short looks complete.
- `Incremental`'s `key=` recognises the records seen at the last value by a field; without it, by their whole content, which also notices a record edited twice in the same instant. `Changes` always matches records by its `key`.
- A record without the tracked field stops the run (`missing="raise"`); pass `missing="keep"` or `"drop"` to allow it.
- `Changes` needs a run that fetches every record: on a filtered fetch, the records not fetched would look deleted.
- Make one tracker per run, or let `tracked_run` make it.
- Read the records inside the `with tracked_run(...)` block, by storing them or with `list()`. `records()` is lazy: records read after the block ends were never part of the tracked run, so its state doesn't move and the next run fetches them again.
