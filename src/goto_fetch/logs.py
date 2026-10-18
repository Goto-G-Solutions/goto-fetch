"""Logging: silent unless switched on, one switch, and events every handler can read.

The package logs under "goto_fetch", each module under its own name
(goto_fetch.client, goto_fetch.paging and so on), and prints nothing
until the application asks:

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
| withheld    | WARNING | method, url, base_url                             |
| stop        | DEBUG   | reason                                            |
| renew       | DEBUG   | how ("refresh" or "login"), why                   |
| no-count    | WARNING | count_key                                         |
| no-next-key | WARNING | key                                               |
| incremental | DEBUG   | kept, dropped, last                               |
| changes     | DEBUG   | new, changed, unchanged, deleted                  |
| count-short | WARNING | records, count                                    |
| text        | WARNING | column, reason                                    |
| widen       | WARNING | table, column, type                               |
| stored      | INFO    | table, records                                    |
| request     | INFO    | method, url, status, seconds, error (logger goto_fetch.http, shown with http=True) |

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
from dataclasses import dataclass
from typing import Any

from .errors import redact

ROOT = "goto_fetch"
"""The package's logger; every module logs under it."""

HTTP = ("goto_fetch.http", "httpx2")
"""The loggers of each request sent: the package's "request" events, and
httpx2's own lines when Httpx2Transport sends. enable_logging(http=True)
shows them; otherwise they are held back."""

# A library prints nothing on its own: the application decides (see the
# Python logging HOWTO, "Configuring logging for a library").
logging.getLogger(ROOT).addHandler(logging.NullHandler())

_client_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "goto_fetch_client", default=None
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
        safe_args = tuple(redact(a) if isinstance(a, str) else a for a in args)
        safe_data = {
            k: redact(v) if isinstance(v, str) else v for k, v in (data or {}).items()
        }
        logger.log(
            level, message, *safe_args, extra={"event": event, "data": safe_data}
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


class RedactFilter(logging.Filter):
    """Hides secrets in URLs in each record's message (see redact_url).

    enable_logging puts this on its handler, so lines from httpx2 (when it
    sends) and from your own pieces are covered too, not just the package's
    events.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        safe = redact(message)
        if safe != message:
            record.msg, record.args = safe, None
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


_ATTACHED = "_goto_fetch_attached"
"""The attribute enable_logging marks a handler with, to undo its changes."""


@dataclass
class _Attached:
    """What enable_logging changed, so disable_logging can put it back."""

    levels: dict[str, int]
    handler_level: int
    filters: list[logging.Filter]
    formatter_added: bool


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
    pieces you build, and `http` adds a "request" event per request sent
    (and httpx2's own line, when it sends). The handler stamps each record
    with record.client (see ClientFilter) and hides secrets in URLs (see
    RedactFilter). It is returned, so disable_logging(handler) can take it
    off again.
    """
    number = _level(level)
    handler = handler or logging.StreamHandler(sys.stderr)
    if hasattr(handler, _ATTACHED):
        raise ValueError(
            "This handler is already attached; disable_logging(handler) first"
        )
    filters = [ClientFilter(), RedactFilter()]
    attached = _Attached(
        levels={},
        handler_level=handler.level,
        filters=filters,
        formatter_added=handler.formatter is None,
    )
    if attached.formatter_added:
        handler.setFormatter(_Formatter())
    handler.setLevel(number)
    for one in filters:
        handler.addFilter(one)
    for name in [ROOT, *loggers, *(HTTP if http else [])]:
        logger = logging.getLogger(name)
        attached.levels.setdefault(name, logger.level)
        # Lower the logger's level if needed, never raise it: another
        # handler may already want more than this one does.
        logger.setLevel(min(logger.getEffectiveLevel(), number))
        # A logger under the package's reaches the handler through ROOT;
        # giving it the handler too would log each line twice.
        if name == ROOT or not name.startswith(f"{ROOT}."):
            logger.addHandler(handler)
    if not http:
        # The request lines come from a logger under ROOT; hold them back.
        quiet = logging.getLogger(HTTP[0])
        attached.levels.setdefault(HTTP[0], quiet.level)
        quiet.setLevel(max(quiet.getEffectiveLevel(), logging.WARNING))
    setattr(handler, _ATTACHED, attached)
    return handler


def disable_logging(handler: logging.Handler) -> None:
    """Take off a handler enable_logging put on, and undo what it changed.

    The loggers get their levels back, and the handler loses the filters,
    formatter and level enable_logging gave it, so a handler of yours is
    left as it was. A handler that isn't attached is left alone.
    """
    attached: _Attached | None = getattr(handler, _ATTACHED, None)
    if attached is None:
        return
    for name, level in attached.levels.items():
        logger = logging.getLogger(name)
        logger.removeHandler(handler)
        logger.setLevel(level)
    for one in attached.filters:
        handler.removeFilter(one)
    if attached.formatter_added:
        handler.setFormatter(None)
    handler.setLevel(attached.handler_level)
    delattr(handler, _ATTACHED)


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
