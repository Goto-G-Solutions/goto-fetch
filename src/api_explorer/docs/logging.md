# Logging

The package logs nothing until you ask. Then every line is an event with data, so a handler can act on it without reading the text.

## Examples

```python
import logging
from api_explorer import enable_logging, disable_logging, logging_to
from api_explorer.testing import next_link_api

handler = enable_logging("INFO")  # pages and a summary, on stderr
disable_logging(handler)  # off again, levels and handler as they were

events = []


class Collect(logging.Handler):
    def emit(self, record):
        events.append((record.event, record.data))


with logging_to(Collect(), "INFO"):
    list(next_link_api(150, page_size=100).client(name="shop").records("/items/"))

assert [event for event, _ in events] == ["page", "page", "done"]
assert events[-1][1] == {
    "pages": 2,
    "records": 150,
    "seconds": events[-1][1]["seconds"],
}
```

`enable_logging("DEBUG", http=True)` adds every decision (why paging stopped, why a token was renewed) and httpx2's line per request: the first thing to turn on when a run is slow or stops early.

## What it handles for you

- **Secrets:** URLs in every line, httpx2's included, have their secrets hidden.
- **Which client:** `Client(name="shop")` stamps `record.client` on every line logged during its requests, including lines from pieces of your own.

## The events

The full table, with levels and data keys, is in the `api_explorer.logs` docstring (`python -c "import api_explorer.logs as l; help(l)"`). The ones to know: `page` and `done` (INFO, progress), `retry` (WARNING), `stop` (DEBUG, why paging ended), `renew` (DEBUG), `no-count`, `count-short`, `text` and `widen` (WARNING, data that didn't fit), `stored` (INFO).

A piece of your own logs the same way, with `log_event(logger, level, event, message, *args, data={...})` under a logger in the `api_explorer` tree or one you pass to `enable_logging(loggers=[...])`.

## Traps

- Attaching one handler twice is refused; `disable_logging` it first.
- `logging_to` is for a block of code; `enable_logging` until `disable_logging`.
