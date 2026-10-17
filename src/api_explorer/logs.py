"""Logging: silent unless switched on, one switch, and events every handler can read.

The package logs under "api_explorer" (api_explorer.client, .paging, .auth,
.frames) and prints nothing until the application asks:

    enable_logging()                      # INFO and up, on stderr
    enable_logging("DEBUG", http=True)    # every decision, and each request
    with logging_to(my_handler):          # for a while, to a handler of yours
        ...

Every record the package logs carries `record.event`, a short name, and
`record.data`, a dict of plain values, so a handler can act on events
without reading the text:

| event       | level   | data                                              |
|-------------|---------|---------------------------------------------------|
| page        | INFO    | page, records_on_page, records_so_far, total, seconds |
| done        | INFO    | pages, records, seconds                           |
| retry       | WARNING | method, url, attempt, wait, status                |
| give-up     | DEBUG   | method, url, attempt, status                      |
| stop        | DEBUG   | reason                                            |
| renew       | DEBUG   | how ("refresh" or "login"), why                   |
| no-count    | WARNING | count_key                                         |
| count-short | WARNING | records, count                                    |
| text        | WARNING | column, reason                                    |

`record.client` is the name of the Client whose request was running when
the record was logged (Client(name=...)), or None. The handlers that
enable_logging and logging_to attach set it; add ClientFilter to a handler
of your own for the same. Because the name travels in a context variable,
it reaches log lines from pieces you build (a paging style, a retry policy,
an auth) as long as they log during the request, under a logger the
handler covers.
"""

import contextvars
import logging
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

ROOT = "api_explorer"
"""The package's logger; every module logs under it."""

HTTP = "httpx2"
"""httpx2's logger, which logs one INFO line per request."""

# A library prints nothing on its own: the application decides (see the
# Python logging HOWTO, "Configuring logging for a library").
logging.getLogger(ROOT).addHandler(logging.NullHandler())

_client_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "api_explorer_client", default=None
)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    *args: Any,
    data: Mapping[str, Any] | None = None,
) -> None:
    """Log one event, with `record.event` and `record.data` set.

    The package logs this way, and pieces you build can too, so their lines
    look the same to a handler. `message` and `args` work as in logging.
    """
    if logger.isEnabledFor(level):
        logger.log(
            level, message, *args, extra={"event": event, "data": dict(data or {})}
        )


@contextmanager
def client_context(name: str | None) -> Iterator[None]:
    """Mark what runs inside the block as the work of the client named `name`."""
    if name is None:
        yield
        return
    token = _client_name.set(name)
    try:
        yield
    finally:
        _client_name.reset(token)


class ClientFilter(logging.Filter):
    """Sets record.client to the name of the Client running when the record was logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "client", None) is None:
            record.client = _client_name.get()
        return True


class _Formatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s %(levelname)s %(name)s%(where)s: %(message)s", "%H:%M:%S"
        )

    def format(self, record: logging.LogRecord) -> str:
        client = getattr(record, "client", None)
        record.where = f" [{client}]" if client else ""
        return super().format(record)


def _level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    names = logging.getLevelNamesMapping()
    if level.upper() not in names:
        raise ValueError(
            f"Unknown logging level {level!r}; use one of {', '.join(names)}"
        )
    return names[level.upper()]


def enable_logging(
    level: int | str = "INFO",
    *,
    loggers: Sequence[str] = (),
    http: bool = False,
    handler: logging.Handler | None = None,
) -> logging.Handler:
    """Show this package's log lines from `level` up, on stderr or on `handler`.

    `loggers` adds other loggers to the same handler, such as those of
    pieces you build, and `http` adds httpx2's line per request. The
    handler stamps each record with record.client (see ClientFilter). It
    is returned, so disable_logging(handler) can take it off again.
    """
    number = _level(level)
    handler = handler or logging.StreamHandler(sys.stderr)
    if handler.formatter is None:
        handler.setFormatter(_Formatter())
    handler.setLevel(number)
    handler.addFilter(ClientFilter())
    names = [ROOT, *loggers, *([HTTP] if http else [])]
    previous = {}
    for name in names:
        logger = logging.getLogger(name)
        previous[name] = logger.level
        # Lower the logger's level if needed, never raise it: another
        # handler may already want more than this one does.
        logger.setLevel(min(logger.getEffectiveLevel(), number))
        logger.addHandler(handler)
    handler._api_explorer_previous = previous  # type: ignore[attr-defined]
    return handler


def disable_logging(handler: logging.Handler) -> None:
    """Take off a handler enable_logging put on, and put the loggers' levels back."""
    previous: dict[str, int] = getattr(handler, "_api_explorer_previous", {})
    for name, level in previous.items():
        logger = logging.getLogger(name)
        logger.removeHandler(handler)
        logger.setLevel(level)


@contextmanager
def logging_to(
    handler: logging.Handler | None = None,
    level: int | str = "INFO",
    *,
    loggers: Sequence[str] = (),
    http: bool = False,
) -> Iterator[logging.Handler]:
    """enable_logging for the length of a `with` block, then disable_logging."""
    attached = enable_logging(level, loggers=loggers, http=http, handler=handler)
    try:
        yield attached
    finally:
        disable_logging(attached)
