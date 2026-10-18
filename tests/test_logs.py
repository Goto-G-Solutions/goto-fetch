"""Logging: silent by default, the switch, events with data, and the client name."""

import io
import logging
import subprocess
import sys

import pytest

from api_explorer import (
    ApiError,
    ClientFilter,
    Cursor,
    HeaderLink,
    Keyset,
    Offset,
    PasswordLogin,
    Retry,
    TokenRefresh,
    disable_logging,
    enable_logging,
    http,
    logging_to,
    to_frame,
)
from api_explorer.logs import log_event
from api_explorer.testing import (
    TokenServer,
    cursor_api,
    json_pages,
    keyset_api,
    link_header_api,
    next_link_api,
    offset_api,
    replies,
)


class Collect(logging.Handler):
    """A handler that keeps every record it is given."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def events(self):
        return [(r.event, r.data) for r in self.records if hasattr(r, "event")]


def test_nothing_prints_until_logging_is_switched_on():
    # A fresh interpreter, so no test runner's handlers are in the way.
    script = (
        "from api_explorer.testing import replies\n"
        "replies(503, 200).client().request('GET', '/x/')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert result.stderr == ""


def test_enable_logging_shows_lines_and_disable_logging_takes_them_off():
    stream = io.StringIO()
    handler = enable_logging("WARNING", handler=logging.StreamHandler(stream))
    replies(503, 200).client().request("GET", "/x/")
    disable_logging(handler)
    replies(503, 200).client().request("GET", "/x/")
    assert stream.getvalue().count("retry 1") == 1
    assert "WARNING api_explorer.client:" in stream.getvalue()


def test_enable_logging_lowers_a_logger_s_level_but_never_raises_it():
    logger = logging.getLogger("api_explorer")
    logger.setLevel(logging.DEBUG)
    try:
        handler = enable_logging("INFO", handler=Collect())
        assert logger.level == logging.DEBUG
        disable_logging(handler)
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(logging.NOTSET)


def test_a_logger_under_the_package_s_gets_the_handler_once():
    # Regression: naming "api_explorer.client" in loggers= put the handler
    # on it and on "api_explorer", so each of its lines showed twice.
    collect = Collect()
    with logging_to(collect, "WARNING", loggers=("api_explorer.client",)):
        replies(503, 200).client().request("GET", "/x/")
    assert [event for event, _ in collect.events()] == ["retry"]


def test_disable_logging_restores_a_level_whatever_the_repeats():
    # Regression: a name given twice saved its already-lowered level, so
    # disable_logging put that back instead of the original.
    logger = logging.getLogger("api_explorer")
    logger.setLevel(logging.ERROR)
    try:
        handler = enable_logging("INFO", handler=Collect(), loggers=("api_explorer",))
        disable_logging(handler)
        assert logger.level == logging.ERROR
    finally:
        logger.setLevel(logging.NOTSET)


def test_enable_logging_refuses_a_level_it_doesn_t_know():
    with pytest.raises(ValueError, match="Unknown logging level 'LOUD'"):
        enable_logging("LOUD")


def test_logging_to_collects_only_inside_its_block():
    collect = Collect()
    with logging_to(collect, "INFO"):
        list(next_link_api(3).client().records("/items/"))
    list(next_link_api(3).client().records("/items/"))
    assert [event for event, _ in collect.events()] == ["page", "done"]


def test_http_adds_a_request_event_per_request_sent():
    collect = Collect()
    with logging_to(collect, "INFO", http=True):
        replies(200).client().request("GET", "/x/")
    [request] = [r for r in collect.records if getattr(r, "event", None) == "request"]
    assert request.name == "api_explorer.http"
    assert {k: request.data[k] for k in ("method", "url", "status")} == {
        "method": "GET",
        "url": "http://api.test/api/x/",
        "status": 200,
    }


def test_without_http_the_request_lines_are_held_back():
    collect = Collect()
    with logging_to(collect, "DEBUG"):
        replies(200).client().request("GET", "/x/")
    assert not [r for r in collect.records if getattr(r, "event", None) == "request"]


def run_and_collect(action, level="DEBUG"):
    """Run `action` with every package event collected, and return the events."""
    collect = Collect()
    with logging_to(collect, level):
        action()
    return collect.events()


def test_a_retry_and_a_give_up_are_events_with_their_details():
    events = run_and_collect(
        lambda: pytest.raises(ApiError, replies(503).client().request, "GET", "/x/")
    )
    kinds = [event for event, _ in events]
    assert kinds == ["retry", "retry", "give-up"]
    retry = events[0][1]
    assert {k: retry[k] for k in ("method", "attempt", "status")} == {
        "method": "GET",
        "attempt": 1,
        "status": 503,
    }
    assert retry["wait"] >= 0
    assert events[-1][1]["attempt"] == 3


@pytest.mark.parametrize(
    ("make", "options", "reason"),
    [
        (lambda: next_link_api(3), {}, "no next link"),
        (
            lambda: link_header_api(3),
            {"results_key": None, "paging": HeaderLink()},
            "no next link",
        ),
        (
            lambda: cursor_api(3),
            {"results_key": "data", "paging": Cursor()},
            "no cursor",
        ),
        (
            lambda: offset_api(3),
            {"results_key": "items", "count_key": "count", "paging": Offset(limit=2)},
            "count reached",
        ),
        (
            lambda: offset_api(4, count=False),
            {"results_key": "items", "paging": Offset(limit=2)},
            "empty page",
        ),
        (
            lambda: offset_api(3, count=False),
            {"results_key": "items", "paging": Offset(limit=2)},
            "short page",
        ),
        (
            lambda: keyset_api([]),
            {"paging": Keyset("updated_at", "updated_since")},
            "empty page",
        ),
    ],
    ids=[
        "next-link",
        "link-header",
        "cursor",
        "offset-count",
        "offset-empty",
        "offset-short",
        "keyset-empty",
    ],
)
def test_each_paging_style_says_why_it_stopped(make, options, reason):
    api = make()
    events = run_and_collect(lambda: list(api.client().records("/items/", **options)))
    assert [data["reason"] for event, data in events if event == "stop"] == [reason]


def test_max_pages_is_a_stop_reason_too():
    events = run_and_collect(
        lambda: list(
            next_link_api(9, page_size=2).client().records("/items/", max_pages=1)
        )
    )
    assert [data["reason"] for event, data in events if event == "stop"] == [
        "max_pages"
    ]


def test_each_token_renewal_is_an_event_saying_how_and_why():
    server = TokenServer()
    auth = TokenRefresh(
        "/token/refresh/", refresh="expired", login=PasswordLogin("/token/", "me", "pw")
    )
    events = run_and_collect(lambda: list(server.client(auth=auth).records("/items/")))
    renewals = [data for event, data in events if event == "renew"]
    assert [r["how"] for r in renewals] == ["refresh", "login"]
    assert renewals[0]["why"] == "no access token yet"
    assert "returned HTTP 401" in renewals[1]["why"]


@pytest.mark.parametrize(
    ("action", "event", "data"),
    [
        (
            lambda: list(
                json_pages(
                    {"http://api.test/api/items/": {"results": [], "next": None}}
                )
                .client()
                .records("/items/", count_key="count")
            ),
            "no-count",
            {"count_key": "count"},
        ),
        (
            lambda: list(
                offset_api(3, count=5)
                .client()
                .records(
                    "/items/",
                    results_key="items",
                    count_key="count",
                    paging=Offset(limit=2),
                )
            ),
            "count-short",
            {"records": 3, "count": 5},
        ),
        (
            lambda: to_frame([{"v": 1}, {"v": "x"}], "polars"),
            "text",
            {"column": "v", "reason": "mixes number, str"},
        ),
    ],
    ids=["no-count", "count-short", "text"],
)
def test_warnings_are_events_with_their_details(action, event, data):
    pytest.importorskip("polars")
    assert (event, data) in run_and_collect(action, "WARNING")


def test_the_client_name_reaches_lines_logged_by_your_own_pieces():
    # A custom paging style logging under its own logger: during the
    # request, its line carries the client's name like the package's do.
    mine = logging.getLogger("mypkg.paging")

    class Chatty:
        def begin(self, first, read):
            def next_link(fetched, request):
                log_event(
                    mine, logging.INFO, "my-step", "Looked at a page", data={"n": 1}
                )
                return

            return first, next_link

    collect = Collect()
    with logging_to(collect, "INFO", loggers=("mypkg",)):
        list(next_link_api(2).client(name="crm").records("/items/", paging=Chatty()))
    stamped = {(r.name, r.client) for r in collect.records}
    assert ("mypkg.paging", "crm") in stamped
    assert ("api_explorer.client", "crm") in stamped
    assert ("my-step", {"n": 1}) in collect.events()


def test_lines_outside_a_named_client_s_requests_carry_no_name():
    collect = Collect()
    with logging_to(collect, "INFO"):
        list(next_link_api(2).client().records("/items/"))
    assert {r.client for r in collect.records} == {None}


def test_the_readable_format_shows_the_client_name():
    stream = io.StringIO()
    with logging_to(logging.StreamHandler(stream), "INFO"):
        list(next_link_api(2).client(name="crm").records("/items/"))
    assert "INFO api_explorer.client [crm]: Page 1:" in stream.getvalue()


def test_client_filter_stamps_records_for_a_handler_of_your_own():
    collect = Collect()
    collect.addFilter(ClientFilter())
    logger = logging.getLogger("api_explorer")
    logger.addHandler(collect)
    logger.setLevel(logging.INFO)
    try:
        list(next_link_api(2).client(name="billing").records("/items/"))
    finally:
        logger.removeHandler(collect)
        logger.setLevel(logging.NOTSET)
    assert {r.client for r in collect.records} == {"billing"}


def test_disabling_logging_leaves_a_handler_of_yours_as_it_was():
    # Regression: the filters, formatter and level enable_logging gave a
    # handler stayed on it after disable_logging.
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.ERROR)
    with logging_to(handler, "DEBUG"):
        assert handler.filters
    assert (handler.filters, handler.formatter, handler.level) == (
        [],
        None,
        logging.ERROR,
    )


def test_a_handler_can_t_be_attached_twice():
    handler = logging.StreamHandler(io.StringIO())
    with logging_to(handler), pytest.raises(ValueError, match="already attached"):
        enable_logging(handler=handler)
    assert handler.filters == []


def test_a_send_that_fails_still_logs_its_request_event():
    collect = Collect()
    client = replies(http.ConnectError("refused")).client(retry=Retry(attempts=1))
    with logging_to(collect, "INFO", http=True), pytest.raises(ApiError):
        client.request("GET", "/x/")
    [request] = [r for r in collect.records if getattr(r, "event", None) == "request"]
    assert (request.data["status"], request.data["error"]) == (
        None,
        "ConnectError: refused",
    )
