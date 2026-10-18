# Build your own piece

Every piece you might replace sits behind a small interface, and the built-in one is just one implementation. To build your own:

1. **Match the shape**, a few methods or one function. Don't inherit from our classes: interfaces are `Protocol`s, so any object with the right methods fits, and a type checker confirms it.
2. **Keep its promises**, listed below for each piece.
3. **Run its check** from `api_explorer.testing`, where there is one: it tries each promise and names the first one broken.
4. **Leave the rest to the package.** Each section says what you get without writing it.

| To change... | Implement | Check |
|--------------|-----------|-------|
| how the next page is found | `Paging`: `begin(first, read)` | `check_paging` |
| when to retry | `RetryPolicy`: `wait(failure)` | `check_retry_policy` |
| how tokens are fetched | `Login`: `token_url`, `build_request(url)` | |
| how requests are sent | `Transport`: `send(request, timeout)`, `close()` | `check_transport` |
| how requests are signed | `Auth`: `auth_flow(request)` | |
| what counts as a failed 200 | `ResponseCheck`: a function | |
| how JSON is parsed | `Decoder`: a function | |
| what a run delivers | `Tracker`: `state`, `apply`, `track` | `check_tracker` |
| what counts as a change | `Fingerprint`: a function | |
| where run state lives | `StateStore`: `get`, `put` | `check_state_store` |
| where records live | `Warehouse`: `write`, `read` | `check_warehouse` |
| an SQL database as a warehouse | `SqlTables`, with `write_run` | `check_warehouse` |
| how a scraped field is read | `FieldExtractor`: `extract_value` | |

## A paging style

`begin(first, read)` gets the first `PageRequest` and a `read(fetched)` that gives a page's records and count. It returns the request to fetch first and a `next_link(fetched, request)` that answers with a URL, a `PageRequest`, or `None` at the end.

**Promises:** stop eventually; never ask for a page already fetched; get every record once.

**You get for free:** fetching, retries, auth, reading the records, loop detection, page events, and `records()`'s whole interface.

```python
from api_explorer import with_param
from api_explorer.testing import check_paging, next_link_api


class PageNumber:
    """?page=1, 2, 3... until a page comes back empty."""

    def begin(self, first, read):
        number = 1

        def next_link(fetched, request):
            nonlocal number
            if not read(fetched).items:
                return None
            number += 1
            return with_param(request, "page", number)

        return first, next_link


check_paging(PageNumber(), next_link_api(250), list(range(250)))
```

`with_param(request, name, value, send_in="query" or "body")` sets a parameter in either place.

## A retry policy

`wait(failure)` returns the seconds to wait before the next try, or `None` to stop. `failure` has `attempt`, `method`, `url`, and one of `response` or `error`.

**Promises:** stop eventually; never ask for a negative, endless or non-number wait.

**You get for free:** the loop that waits and tries again, logging, and the final `ApiError`.

```python
from api_explorer.testing import check_retry_policy


class FixedWait:
    """Up to `attempts` tries, `seconds` apart, for any failure but a 4xx."""

    def __init__(self, seconds=1.0, attempts=4):
        self.seconds, self.attempts = seconds, attempts

    def wait(self, failure):
        status = failure.response.status_code if failure.response is not None else None
        if failure.attempt >= self.attempts or (status and 400 <= status < 500):
            return None
        return self.seconds


check_retry_policy(FixedWait())
```

## A transport

A transport sends one request and gives back the response: `send(request, timeout)` and `close()`. `Request` and `Response` come from `api_explorer.http`, and so do the errors to raise.

**Promises:** what is sent arrives as sent; the response is the server's (status, headers by any case, body unpacked); an error status is returned, never raised; redirects are followed; a refused connection or a timeout raises `NetworkError` (or a subclass: `ConnectError`, `TimeoutError`, `ConnectionDropped`), and anything else that sent nothing raises `TransportError`.

**You get for free:** retries, auth and token renewal, paging, logging, and every piece above it, whatever library sends.

A transport can also wrap another, to add something to every request:

```python
from api_explorer import Urllib3Transport
from api_explorer.testing import check_transport


class Counting:
    """Counts the requests another transport sends."""

    def __init__(self, inner):
        self.inner, self.sent = inner, 0

    def send(self, request, timeout):
        self.sent += 1
        return self.inner.send(request, timeout)

    def close(self):
        self.inner.close()


check_transport(lambda: Counting(Urllib3Transport()))  # over real HTTP, on this machine
```

For another library, translate its exceptions to those three meanings, follow redirects, and unpack the body; `api_explorer/urllib3_transport.py` is a short model.

## A login

A login has a `token_url` and `build_request(url)`, which builds the request that gets tokens; `TokenRefresh` sends it and reads the reply.

**You get for free:** renewal before expiry and after a 401, safety across threads, URL resolution, and retries at the token endpoint.

```python
from api_explorer import http
from api_explorer import Client, TokenRefresh
from api_explorer.testing import TokenServer


class TeamLogin:
    """A JSON login that also sends the team's header."""

    token_url = "/token/"

    def build_request(self, url):
        body = {"username": "me", "password": "pw"}
        return http.Request("POST", url, json=body, headers={"X-Team": "data"})


server = TokenServer()
client = Client(
    "http://api.test/api",
    auth=TokenRefresh(login=TeamLogin()),
    transport=server,
)
assert list(client.records("/items/")) == [{"id": 1}]
```

## A signing auth

An `Auth` has `auth_flow(request)`, a generator as in httpx: it yields the request to send, signed, and receives the response; it may yield more requests (a token first, or the request again after a 401). Pass it as `Client(auth=...)`; every request it yields goes through the client's transport.

```python
import hashlib
import hmac
from api_explorer.testing import replies


class Signed:
    def __init__(self, secret: bytes):
        self.secret = secret

    def auth_flow(self, request):
        digest = hmac.new(self.secret, request.url.path.encode(), hashlib.sha256)
        request.headers["X-Signature"] = digest.hexdigest()
        yield request


api = replies(200)
api.client(auth=Signed(b"key")).request("GET", "/items/")
assert "X-Signature" in api.calls[0].headers
```

## A response check

A function from a successful response to `None` (fine), a message (a permanent problem) or `Problem(message, temporary=True)` (worth retrying). Pass it as `Client(check=...)`.

```python
from api_explorer import ApiError, Problem


def status_is_ok(response):
    body = response.json()
    if body.get("status") == "busy":
        return Problem("the API is busy", temporary=True)
    return None if body.get("status") == "ok" else f"status is {body.get('status')!r}"


api = replies(http.Response(200, json={"status": "failed"}))
try:
    api.client(check=status_is_ok).get_json("/items/")
except ApiError as error:
    assert "status is 'failed'" in str(error)
```

## A decoder

A function from bytes to parsed JSON, raising `ValueError` on bad input. Pass it as `Client(decode=...)`.

```python
import json


def strip_guard(body: bytes):
    """Some APIs prefix JSON with a guard against script inclusion."""
    return json.loads(body.removeprefix(b")]}'\n"))


api = replies(http.Response(200, content=b')]}\'\n{"ok": true}'))
assert api.client(decode=strip_guard).get_json("/x/") == {"ok": True}
```

## A tracker

A tracker has `state` (plain JSON to save), `apply(request)` (change the first request, or return it as is) and `track(records)` (yield what this run delivers). Construct it from the last state as `state=`, so `tracked_run` can build it. Add a `deleted` property to fit `ReportsDeletions`, so a warehouse removes those rows.

**Promises:** state is plain JSON, and moves on only when `track` has seen every record; a failure of the records reaches the caller; a first run delivers every record; a second run over unchanged records delivers none.

**You get for free:** saving and loading the state at the right moment (`tracked_run`), and storing with deletions.

```python
from api_explorer.testing import check_tracker


class NewIds:
    """Delivers records whose id hasn't been seen: for data that never changes."""

    def __init__(self, state=None):
        self._ids = list((state or {}).get("ids", []))
        self._state = {"ids": list(self._ids)}
        self.deleted = None

    @property
    def state(self):
        return dict(self._state)

    def apply(self, request):
        return request

    def track(self, records):
        seen = []
        for record in records:
            seen.append(record["id"])
            if record["id"] not in self._ids:
                yield record
        self._state = {"ids": seen}  # only now, after every record
        self.deleted = [i for i in self._ids if i not in seen]


check_tracker(NewIds, [{"id": 1}, {"id": 2}, {"id": 3}])
```

`Changes(key, fingerprint=...)` takes a function of your own in place of its hash, such as a record's version.

## A state store

`get(name)` gives the value last saved, or `None`; `put(name, value)` replaces it. Values are plain JSON.

**Promises:** `get` gives a copy, not the stored object; `put` replaces; names don't affect each other.

```python
import os
from pathlib import Path
from api_explorer.testing import check_state_store


class JsonFileState:
    """One JSON file per name, replaced in one step so a crash can't leave half a file."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(exist_ok=True)

    def get(self, name):
        path = self.folder / f"{name}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def put(self, name, value):
        path = self.folder / f"{name}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        os.replace(temporary, path)


check_state_store(lambda: JsonFileState(f"state-{os.urandom(4).hex()}"))
```

## A warehouse

`write(table, records, *, key=None, deleted_by=None)` stores a run and returns how many records it stored; `read(table, limit=None)` gives the rows as dicts.

**Promises:** one write is all or nothing; with a key, a record replaces the row with its key, the last of a key winning; `deleted_by`'s keys are removed only after storing, and a tracker whose run isn't complete is refused; no value is lost (`read` gives it back as written, or as its JSON text); a record without its key is refused.

**You get for free:** tracked runs into it (`tracked_run`, `FetchPlan.store(warehouse=...)`), and the checks. `check_write`, `deleted_keys` and `last_per_key` from `api_explorer.warehouse` handle the argument rules.

```python
from api_explorer.testing import check_warehouse
from api_explorer.warehouse import check_write, deleted_keys, last_per_key


class JsonLinesWarehouse:
    """One JSON-lines file per table, replaced whole on each write."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(exist_ok=True)

    def read(self, table, limit=None):
        path = self.folder / f"{table.encode().hex()}.jsonl"
        rows = (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )
        names = list(dict.fromkeys(n for row in rows for n in row))
        full = [{n: row.get(n) for n in names} for row in rows]
        return full if limit is None else full[:limit]

    def write(self, table, records, *, key=None, deleted_by=None):
        check_write(key, deleted_by)
        incoming = [
            dict(record) for record in records
        ]  # read all first: all or nothing
        if key is not None:
            incoming = last_per_key(incoming, key)
        gone = deleted_keys(deleted_by)
        rows = [
            {k: v for k, v in row.items() if v is not None} for row in self.read(table)
        ]
        if key is not None:
            replaced = {json.dumps(row[key]) for row in incoming}
            rows = [row for row in rows if json.dumps(row.get(key)) not in replaced]
        rows += incoming
        rows = [row for row in rows if row.get(key) not in gone]
        path = self.folder / f"{table.encode().hex()}.jsonl"
        temporary = path.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(row) + "\n" for row in rows))
        os.replace(temporary, path)
        return len(incoming)


check_warehouse(lambda: JsonLinesWarehouse(f"shop-{os.urandom(4).hex()}"))
```

### An SQL database

For an SQL database, don't write the promises again: implement `SqlTables` and call `write_run`, which owns the order that keeps them (stage the run to disk, change columns, store rows, remove deletions) and decides each column's kind. `SqlTables` has five methods: `transaction()` (a DB-API connection, committed or rolled back), `columns(connection, table)`, `alter(...)`, `load(connection, table, part, fields, columns, key)` and `delete(connection, table, column, keys)`. The SQLite warehouse is about a hundred lines on it; read `api_explorer/sqlite_warehouse.py` as the model.

```python skip
from api_explorer import SqlTables, write_run


class PostgresWarehouse:
    def __init__(self, dsn):
        self._tables = _PostgresTables(dsn)  # your SqlTables

    def write(self, table, records, *, key=None, deleted_by=None):
        return write_run(self._tables, table, records, key=key, deleted_by=deleted_by)

    def read(self, table, limit=None): ...
```

Then `check_warehouse(lambda: PostgresWarehouse(fresh_database()))`.

## A scraped field

Any object with `extract_value(element)`, which gets a parsel `Selector` for one item and returns text or `None`.

```python skip
class DataAttribute:
    def __init__(self, name):
        self.name = name

    def extract_value(self, element):
        return element.attrib.get(f"data-{self.name}")
```

## Contributing a piece

A piece that would serve others belongs in the package: open an issue first (see `CONTRIBUTING.md`), and update these guides with it.
