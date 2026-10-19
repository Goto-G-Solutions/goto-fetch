"""One fetch, described once: run it, or write it as a standalone script."""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Literal

from .auth import ClientCredentials, PasswordLogin, TokenRefresh, token_headers
from .calls import LINE, Call, Name, Secret, build, names, reads_environment
from .checks import error_at
from .client import Client
from .duckdb_warehouse import DuckDBWarehouse
from .errors import redact_url
from .incremental import Changes, Incremental, ReportsDeletions, Tracker
from .limits import RateLimit
from .paging import RESULTS, Cursor, HeaderLink, Keyset, NextLink, Offset, Paging
from .sqlite_warehouse import SqliteWarehouse
from .state import StateStore, tracked_run
from .warehouse import Warehouse


@dataclass(frozen=True)
class FetchPlan:
    """One fetch, as the explorer notebook describes it.

    client() and records() run it, and store() runs it into a warehouse.
    script() writes it as a PEP 723 script, with uv-installable
    dependencies, that does the same. Each piece is described once, as a
    Call (client_call, paging_call, tracker_call, warehouse_call), and both
    run and written from that description, so a script can't drift from
    what the plan runs.

    Values that identify a person or give access are used to run, kept out
    of repr, and never written: scripts read $API_TOKEN,
    $API_REFRESH_TOKEN, $API_ACCESS_TOKEN (optional), $API_USERNAME,
    $API_PASSWORD and $API_CLIENT_SECRET instead. App settings, such as
    client_id, are written as given. URLs are written as given too, so
    keep secrets out of them: use header auth, not ?api_key=.

    Storing: `warehouse` (a file, DuckDB or SQLite as `warehouse_kind`
    says) and `table` say where records go, and `key` which field
    identifies one; store() also takes any Warehouse of your own. `track`
    chooses what a run delivers: everything ("none"), what changed by a
    field ("incremental": `change_field`, filtered by `change_param` sent
    in `change_in`, if the API can), or what changed by a hash of each
    record ("changes", which needs `key` and can `ignore` fields). A
    tracked plan stores into a warehouse (store() and script() refuse to
    run one without), since saving where a run stopped without storing its
    records would skip them, and can't use max_pages.

    paging="keyset" pages by the same `change_field`, `change_param` and
    `change_in`, with `limit` records a page.

    `rate_limit` keeps every request to that many a second (0, the default,
    for no limit), for servers that can't take a fast run.
    """

    base_url: str
    endpoint: str
    method: str = "GET"
    body: Any = None
    retry: bool | None = None
    trailing_slash: bool = False
    timeout: float = 20.0
    rate_limit: float = 0.0
    auth: Literal["none", "header", "refresh", "client"] = "none"
    token: str = field(default="", repr=False)
    scheme: str | None = "Token"
    header: str = "Authorization"
    refresh_url: str = "/token/refresh/"
    refresh_token: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    login_url: str = "/token/"
    username: str = ""
    password: str = field(default="", repr=False)
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    scope: str = ""
    credentials_in: Literal["header", "body"] = "header"
    results_key: str | None = RESULTS
    count_key: str | None = None
    error_path: str = ""
    paging: Literal["next", "offset", "header", "cursor", "keyset"] = "next"
    next_key: str = "next"
    limit: int = 100
    cursor_key: str = "next_cursor"
    cursor_param: str = "cursor"
    cursor_in: Literal["query", "body"] = "query"
    max_pages: int | None = None
    track: Literal["none", "incremental", "changes"] = "none"
    change_field: str = "updated_at"
    change_param: str = ""
    change_in: Literal["query", "body"] = "query"
    key: str = ""
    ignore: tuple[str, ...] = ()
    warehouse: str = ""
    warehouse_kind: Literal["duckdb", "sqlite"] = "duckdb"
    table: str = ""

    def __post_init__(self) -> None:
        if (self.warehouse or self.track != "none") and not self.table:
            raise ValueError(
                "A plan that stores or tracks needs a table name: where its "
                "records, and where its runs stopped, are kept"
            )
        if self.track == "changes" and not self.key:
            raise ValueError("track='changes' needs a key, such as 'id'")
        if self.track != "none" and self.max_pages:
            raise ValueError(
                "A tracked plan fetches every page: set max_pages to None (0 in "
                "the explorer), or a run cut short would skip records"
            )
        if self.rate_limit < 0:
            raise ValueError(f"rate_limit can't be negative, got {self.rate_limit}")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            # A plan is written into scripts, which take a number here.
            raise TypeError(
                f"A plan's timeout is a number of seconds, got {self.timeout!r}"
            )
        if self.paging == "keyset" and not self.change_param:
            raise ValueError(
                "Keyset paging needs change_param, the filter it pages by, such "
                "as 'updated_since'"
            )

    def client_call(self) -> Call:
        """The Client this plan uses, described once (see goto_fetch.calls)."""
        options: dict[str, Any] = {}
        if self.auth == "header" and self.token:
            options["headers"] = Call(
                token_headers,
                (Secret("API_TOKEN", self.token), self.scheme, self.header),
            )
        elif self.auth == "refresh":
            options["auth"] = self._refresh_call()
        elif self.auth == "client":
            login = Call(
                ClientCredentials,
                (
                    self.login_url,
                    self.client_id,
                    Secret("API_CLIENT_SECRET", self.client_secret),
                ),
                {
                    **({"scope": self.scope} if self.scope else {}),
                    **(
                        {"credentials_in": self.credentials_in}
                        if self.credentials_in != "header"
                        else {}
                    ),
                },
            )
            options["auth"] = Call(TokenRefresh, kwargs={"login": login})
        options["timeout"] = self.timeout
        if self.rate_limit:
            options["rate_limit"] = Call(RateLimit, (self.rate_limit,))
        if self.trailing_slash:
            options["trailing_slash"] = True
        if self.error_path:
            options["check"] = Call(error_at, (self.error_path,))
        return Call(Client, (self.base_url,), options)

    def _refresh_call(self) -> Call:
        options: dict[str, Any] = {}
        if self.refresh_token:
            options["refresh"] = Secret("API_REFRESH_TOKEN", self.refresh_token)
        if self.access_token:
            options["access"] = Secret(
                "API_ACCESS_TOKEN", self.access_token, optional=True
            )
        if self.username and self.password:
            options["login"] = Call(
                PasswordLogin,
                (
                    self.login_url,
                    Secret("API_USERNAME", self.username),
                    Secret("API_PASSWORD", self.password),
                ),
            )
        return Call(TokenRefresh, (self.refresh_url,), options)

    def paging_call(self) -> Call:
        """The paging style this plan asks for, described once."""
        if self.paging == "offset":
            return Call(Offset, kwargs={"limit": self.limit})
        if self.paging == "header":
            return Call(HeaderLink)
        if self.paging == "cursor":
            return Call(
                Cursor,
                kwargs={
                    "key": self.cursor_key,
                    "param": self.cursor_param,
                    "send_in": self.cursor_in,
                },
            )
        if self.paging == "keyset":
            options: dict[str, Any] = {"limit": self.limit}
            if self.change_in != "query":
                options["send_in"] = self.change_in
            return Call(Keyset, (self.change_field, self.change_param), options)
        return Call(NextLink, (self.next_key,) if self.next_key != "next" else ())

    def records_options(self) -> dict[str, Any]:
        """The arguments records() gets besides the endpoint, defaults left out."""
        options: dict[str, Any] = {}
        if self.method.upper() != "GET":
            options["method"] = self.method.upper()
        if self.body is not None:
            options["json"] = self.body
        if self.retry is not None:
            options["retry"] = self.retry
        if self.results_key != RESULTS:
            options["results_key"] = self.results_key
        if self.count_key:
            options["count_key"] = self.count_key
        paging = self.paging_call()
        if paging != Call(NextLink):
            options["paging"] = paging
        if self.max_pages:
            options["max_pages"] = self.max_pages
        return options

    def tracker_call(self) -> Call | None:
        """The tracker this plan uses, or None, with its arguments for tracked_run.

        Incremental recognises records by content, the safer default (see
        Incremental's `key`).
        """
        if self.track == "incremental":
            options = {"send_in": self.change_in} if self.change_in != "query" else {}
            args = (
                self.change_field,
                *([self.change_param] if self.change_param else []),
            )
            return Call(Incremental, args, options)
        if self.track == "changes":
            options: dict[str, Any] = {"key": self.key}
            if self.ignore:
                options["ignore"] = list(self.ignore)
            return Call(Changes, kwargs=options)
        return None

    def warehouse_call(self) -> Call:
        """The warehouse this plan names, a DuckDB or SQLite file, described once."""
        if not self.warehouse:
            raise ValueError(
                "This plan names no warehouse: set warehouse, or pass one to store()"
            )
        kind = SqliteWarehouse if self.warehouse_kind == "sqlite" else DuckDBWarehouse
        return Call(kind, (self.warehouse,))

    def client(self, **options: Any) -> Client:
        """Return a Client set up for this plan; `options` go to Client, over the plan's own.

        For a test, client(transport=fake_api); for a run, the plan's own
        settings are the ones to change.
        """
        call = self.client_call()
        return Call(call.target, call.args, {**call.kwargs, **options}).build()

    def paging_strategy(self) -> Paging:
        """The Paging this plan asks for."""
        return self.paging_call().build()

    def open_warehouse(self) -> Warehouse:
        """The warehouse this plan names."""
        return self.warehouse_call().build()

    def records(
        self, client: Client, incremental: Tracker | None = None
    ) -> Iterator[Any]:
        """Yield this plan's records through `client`, through `incremental` if given."""
        options = {name: build(value) for name, value in self.records_options().items()}
        return client.records(self.endpoint, **options, incremental=incremental)

    def store(
        self,
        client: Client,
        warehouse: Warehouse | None = None,
        state: StateStore | None = None,
    ) -> int:
        """Run this plan into a warehouse, and return how many records were stored.

        The warehouse is `warehouse`, or the one the plan names. A tracked
        plan starts from where its last run stopped, kept in `state` under
        the table's name (by default in the warehouse, if it is also a
        StateStore, as the built-in ones are), and saves where this one
        stopped only once the records are stored.
        """
        shop = warehouse if warehouse is not None else self.open_warehouse()
        key = self.key or None
        tracker = self.tracker_call()
        if tracker is None:
            return shop.write(self.table, self.records(client), key=key)
        keeper = state if state is not None else shop
        if not isinstance(keeper, StateStore):
            raise TypeError(f"{shop!r} can't keep a tracked run's state: pass state=")
        target = tracker.target
        assert not isinstance(target, str)  # a tracker is always a class
        with tracked_run(
            keeper, self.table, target, *tracker.args, **tracker.kwargs
        ) as run:
            return shop.write(
                self.table,
                self.records(client, run),
                key=key,
                deleted_by=run if isinstance(run, ReportsDeletions) else None,
            )

    def script(
        self,
        *,
        columns: Sequence[str] = (),
        backend: str | None = None,
        requirement: str = "goto-fetch",
        requires_python: str = ">=3.11",
    ) -> str:
        """Write this fetch as a script that `uv run` can run, doing what store() or records() do.

        Without a warehouse, `columns` become a select() and `backend` names
        the dataframe library for to_frame(); None leaves plain records.
        With one, the script stores the records as store() does. The PEP 723
        header lists `requirement` (how to install this package; see
        package_requirement), with the "duckdb" extra when it is needed, and
        the backend.
        """
        if self.track != "none" and not self.warehouse:
            raise ValueError(
                "A tracked plan's script needs a warehouse: it would save where "
                "each run stopped without storing the records"
            )
        client = self.client_call()
        records = Call("client.records", (self.endpoint,), self.records_options())
        imports: set[str] = names(client) | names(records)
        if self.warehouse:
            body, used = self._storing_body(client, records)
            imports |= used
            needs_duckdb = self.warehouse_kind == "duckdb"
            deps = [_with_extra(requirement, "duckdb") if needs_duckdb else requirement]
        else:
            body = [
                f"with {client.code()} as client:",
                f"    records = list({records.code(1)})",
            ]
            result = "records"
            if columns:
                imports.add("select")
                body += ["", f"rows = select(records, {list(columns)!r})"]
                result = "rows"
            if backend:
                imports.add("to_frame")
                body += [f"df = to_frame({result}, {backend!r})"]
                result = "df"
            body += [f"print({result})"]
            deps = [requirement, *([backend] if backend else [])]
        header = [
            "# /// script",
            f"# requires-python = {json.dumps(requires_python)}",
            "# dependencies = [",
            *(f"#     {json.dumps(d)}," for d in deps),
            "# ]",
            "# ///",
        ]
        secret = reads_environment(client)
        lines = ["import os", ""] if secret else []
        lines.append(_import_line(sorted(imports)))
        # A literal, so no character in the URLs can break it, with their
        # secrets hidden: a script gets copied and shared.
        doc = repr(
            f"Fetch {redact_url(self.endpoint)} from {redact_url(self.base_url)}, "
            "as set up in the API explorer."
        )
        return "\n".join([*header, doc, "", *lines, "", *body]) + "\n"

    def _storing_body(self, client: Call, records: Call) -> tuple[list[str], set[str]]:
        """The script lines that do what store() does, and the names they use."""
        warehouse = self.warehouse_call()
        tracker = self.tracker_call()
        used = names(warehouse)
        write_options: dict[str, Any] = {}
        if self.key:
            write_options["key"] = self.key
        if tracker is None:
            opening = [f"with {client.code()} as client:"]
        else:
            used |= {"tracked_run"} | names(tracker)
            run = Call(
                tracked_run,
                (Name("shop"), self.table, tracker.target, *tracker.args),
                tracker.kwargs,
            )
            records = Call(
                records.target,
                records.args,
                {**records.kwargs, "incremental": Name("run")},
            )
            if tracker.target is Changes:
                write_options["deleted_by"] = Name("run")
            opening = [
                "with (",
                f"    {client.code(1)} as client,",
                f"    {run.code(1)} as run,",
                "):",
            ]
        write = Call("shop.write", (self.table, records), write_options)
        return [
            f"shop = {warehouse.code()}",
            *opening,
            f"    count = {write.code(1)}",
            # The table goes in as its own literal, never inside the
            # f-string, where a brace or quote in it would break the script.
            f'print(f"Stored {{count:,}} records in", {self.table!r})',
        ], used


def _import_line(imported: list[str]) -> str:
    """`from goto_fetch import ...`, wrapped one name a line when it is long."""
    line = f"from goto_fetch import {', '.join(imported)}"
    if len(line) <= LINE:
        return line
    return "\n".join(
        ["from goto_fetch import (", *(f"    {n}," for n in imported), ")"]
    )


# The rest can't start with a name character: "goto-fetch-tools" is
# another package.
_REQUIREMENT = re.compile(
    r"(?P<name>goto-fetch)(\[(?P<extras>[^\]]*)\])?(?P<rest>(?![\w.-]).*)"
)


def _with_extra(requirement: str, extra: str) -> str:
    """`requirement` for this package with `extra` added, so its version floor has one home.

    "goto-fetch==0.1.0" gives "goto-fetch[duckdb]==0.1.0", and extras
    already there are kept. Another requirement is returned as it is.
    """
    found = _REQUIREMENT.fullmatch(requirement)
    if found is None:
        return requirement
    have = [e.strip() for e in (found["extras"] or "").split(",") if e.strip()]
    extras = ",".join(dict.fromkeys([*have, extra]))
    return f"{found['name']}[{extras}]{found['rest']}"


def package_requirement() -> str:
    """Return how scripts should install this package, matching how it is installed here.

    An editable or local install gives "goto-fetch @ file:///...", one from
    git gives "goto-fetch @ git+URL@commit", and one from an index gives
    "goto-fetch==VERSION". When the package can't be found, it gives the
    bare name.
    """
    try:
        dist = metadata.distribution("goto-fetch")
    except metadata.PackageNotFoundError:
        return "goto-fetch"
    direct = dist.read_text("direct_url.json")
    if not direct:
        return f"goto-fetch=={dist.version}"
    info = json.loads(direct)
    if "vcs_info" in info:
        commit = info["vcs_info"].get("commit_id")
        return f"goto-fetch @ git+{info['url']}" + (f"@{commit}" if commit else "")
    return f"goto-fetch @ {info['url']}"
