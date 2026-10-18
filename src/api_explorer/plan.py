"""One fetch, described once: run it, or write it as a standalone script."""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Literal

from .auth import ClientCredentials, PasswordLogin, TokenRefresh, token_headers
from .checks import error_at
from .client import Client
from .duckdb_warehouse import DuckDBWarehouse
from .incremental import Changes, Incremental, Keyset, ReportsDeletions, Tracker
from .paging import Cursor, HeaderLink, NextLink, Offset, Paging
from .sqlite_warehouse import SqliteWarehouse
from .state import StateStore, tracked_run
from .warehouse import Warehouse


@dataclass(frozen=True)
class FetchPlan:
    """One fetch, as the Explore panel describes it.

    client() and records() run it, and store() runs it into a warehouse.
    script() writes it as a PEP 723 script, with uv-installable
    dependencies, that does the same. Secrets (token, refresh_token,
    access_token, password, client_secret) are used to run, kept out of
    repr, and never written: scripts read $API_TOKEN, $API_REFRESH_TOKEN,
    $API_USERNAME, $API_PASSWORD and $API_CLIENT_SECRET instead.

    Storing: `warehouse` (a file, DuckDB or SQLite as `warehouse_kind`
    says) and `table` say where records go, and `key` which field
    identifies one; store() also takes any Warehouse of your own. `track` chooses what a run
    delivers: everything ("none"), what changed by a field ("incremental":
    `change_field`, filtered by `change_param` if the API can), or what
    changed by a hash of each record ("changes", which needs `key` and can
    `ignore` fields). A tracked plan stores into a warehouse (store() and
    script() refuse to run one without), since saving where a run stopped
    without storing its records would skip them, and can't use max_pages. paging="keyset" pages by `change_field` itself.
    """

    base_url: str
    endpoint: str
    method: str = "GET"
    body: Any = None
    retry: bool | None = None
    trailing_slash: bool = True
    timeout: float = 20.0
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
    results_key: str | None = "results"
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
        if self.paging == "keyset" and not self.change_param:
            raise ValueError(
                "Keyset paging needs change_param, the filter it pages by, such "
                "as 'updated_since'"
            )

    def _login(self) -> PasswordLogin | None:
        if self.username and self.password:
            return PasswordLogin(self.login_url, self.username, self.password)
        return None

    def client(self, **options: Any) -> Client:
        """Return a Client set up for this plan; `options` go to Client."""
        headers: dict[str, str] = {}
        auth = None
        if self.auth == "header" and self.token:
            headers = token_headers(self.token, self.scheme, self.header)
        elif self.auth == "refresh":
            auth = TokenRefresh(
                self.refresh_url,
                refresh=self.refresh_token or None,
                access=self.access_token or None,
                login=self._login(),
            )
        elif self.auth == "client":
            login = ClientCredentials(
                self.login_url,
                self.client_id,
                self.client_secret,
                scope=self.scope or None,
                credentials_in=self.credentials_in,
            )
            auth = TokenRefresh(login=login)
        return Client(
            self.base_url,
            headers=headers,
            auth=auth,
            timeout=self.timeout,
            trailing_slash=self.trailing_slash,
            check=error_at(self.error_path) if self.error_path else None,
            **options,
        )

    def paging_strategy(self) -> Paging:
        """The Paging this plan asks for."""
        if self.paging == "offset":
            return Offset(limit=self.limit)
        if self.paging == "header":
            return HeaderLink()
        if self.paging == "cursor":
            return Cursor(self.cursor_key, self.cursor_param, self.cursor_in)
        if self.paging == "keyset":
            return Keyset(
                self.change_field, self.change_param, self.limit, send_in=self.change_in
            )
        return NextLink(self.next_key)

    def tracker(self) -> tuple[type[Tracker] | None, tuple[Any, ...], dict[str, Any]]:
        """The tracker class this plan uses, with its arguments for tracked_run.

        (None, (), {}) when the plan tracks nothing. Incremental recognises
        records by content, the safer default (see Incremental's `key`).
        """
        if self.track == "incremental":
            args = (self.change_field, self.change_param or None)
            options = {"send_in": self.change_in} if self.change_in != "query" else {}
            return Incremental, args, options
        if self.track == "changes":
            options: dict[str, Any] = {"key": self.key}
            if self.ignore:
                options["ignore"] = list(self.ignore)
            return Changes, (), options
        return None, (), {}

    def open_warehouse(self) -> Warehouse:
        """The warehouse this plan names: a DuckDB or SQLite file."""
        if not self.warehouse:
            raise ValueError(
                "This plan names no warehouse: set warehouse, or pass one to store()"
            )
        if self.warehouse_kind == "sqlite":
            return SqliteWarehouse(self.warehouse)
        return DuckDBWarehouse(self.warehouse)

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
        kind, args, options = self.tracker()
        if kind is None:
            return shop.write(self.table, self.records(client), key=key)
        keeper = state if state is not None else shop
        if not isinstance(keeper, StateStore):
            raise TypeError(f"{shop!r} can't keep a tracked run's state: pass state=")
        with tracked_run(keeper, self.table, kind, *args, **options) as run:
            return shop.write(
                self.table,
                self.records(client, run),
                key=key,
                deleted_by=run if isinstance(run, ReportsDeletions) else None,
            )

    def records(
        self, client: Client, incremental: Tracker | None = None
    ) -> Iterator[Any]:
        """Yield this plan's records through `client`, through `incremental` if given."""
        return client.records(
            self.endpoint,
            method=self.method,
            json=self.body,
            retry=self.retry,
            results_key=self.results_key,
            count_key=self.count_key,
            paging=self.paging_strategy(),
            max_pages=self.max_pages,
            incremental=incremental,
        )

    def script(
        self,
        *,
        columns: Sequence[str] = (),
        backend: str | None = None,
        requirement: str = "api-explorer",
        requires_python: str = ">=3.11",
    ) -> str:
        """Write this fetch as a script that `uv run` can run.

        `columns` become a select(), and `backend` names the dataframe
        library for to_frame(); None leaves plain records. The PEP 723
        header lists `requirement` (how to install this package; see
        package_requirement) and the backend.
        """
        deps = [requirement, *([backend] if backend else [])]
        names = {"Client"}
        body: list[str] = []
        client_args = [repr(self.base_url)]
        if self.auth == "header" and self.token:
            names.add("token_headers")
            client_args.append(
                f'headers=token_headers(os.environ["API_TOKEN"], {self.scheme!r}, '
                f"{self.header!r})"
            )
        elif self.auth == "refresh":
            names.add("TokenRefresh")
            parts = [repr(self.refresh_url)]
            if self.refresh_token:
                parts.append('refresh=os.environ["API_REFRESH_TOKEN"]')
            if self._login():
                names.add("PasswordLogin")
                parts.append(
                    f"login=PasswordLogin({self.login_url!r}, "
                    'os.environ["API_USERNAME"], os.environ["API_PASSWORD"])'
                )
            body += ["auth = TokenRefresh(", *(f"    {p}," for p in parts), ")", ""]
            client_args.append("auth=auth")
        elif self.auth == "client":
            names.update({"TokenRefresh", "ClientCredentials"})
            parts = [
                repr(self.login_url),
                repr(self.client_id),
                'os.environ["API_CLIENT_SECRET"]',
                *([f"scope={self.scope!r}"] if self.scope else []),
                *(
                    [f"credentials_in={self.credentials_in!r}"]
                    if self.credentials_in != "header"
                    else []
                ),
            ]
            body += [
                "auth = TokenRefresh(",
                "    login=ClientCredentials(",
                *(f"        {p}," for p in parts),
                "    ),",
                ")",
                "",
            ]
            client_args.append("auth=auth")
        client_args.append(f"timeout={self.timeout!r}")
        if not self.trailing_slash:
            client_args.append("trailing_slash=False")
        if self.error_path:
            names.add("error_at")
            client_args.append(f"check=error_at({self.error_path!r})")

        record_args = [repr(self.endpoint)]
        if self.method.upper() != "GET":
            record_args.append(f"method={self.method.upper()!r}")
        if self.body is not None:
            record_args.append(f"json={self.body!r}")
        if self.retry is not None:
            record_args.append(f"retry={self.retry!r}")
        record_args.append(f"results_key={self.results_key!r}")
        if self.count_key:
            record_args.append(f"count_key={self.count_key!r}")
        if self.paging == "offset":
            record_args.append(f"paging=Offset(limit={self.limit!r})")
        elif self.paging == "header":
            record_args.append("paging=HeaderLink()")
        elif self.paging == "cursor":
            record_args.append(
                f"paging=Cursor(key={self.cursor_key!r}, param={self.cursor_param!r}, "
                f"send_in={self.cursor_in!r})"
            )
        elif self.paging == "keyset":
            send_in = (
                f", send_in={self.change_in!r}" if self.change_in != "query" else ""
            )
            record_args.append(
                f"paging=Keyset({self.change_field!r}, {self.change_param!r}, "
                f"limit={self.limit!r}{send_in})"
            )
        elif self.next_key != "next":
            record_args.append(f"paging=NextLink({self.next_key!r})")
        if self.max_pages:
            record_args.append(f"max_pages={self.max_pages!r}")
        if any(a.startswith("paging=") for a in record_args):
            names.add(type(self.paging_strategy()).__name__)

        if self.track != "none" and not self.warehouse:
            raise ValueError(
                "A tracked plan's script needs a warehouse: it would save where "
                "each run stopped without storing the records"
            )
        if self.warehouse:
            if self.warehouse_kind == "duckdb":
                deps.append("duckdb")
            body += self._storing_lines(client_args, record_args, names)
            columns, backend = (), None  # the records are in the warehouse
        else:
            body += [
                "with Client(",
                *(f"    {a}," for a in client_args),
                ") as client:",
                "    records = list(",
                "        client.records(",
                *(f"            {a}," for a in record_args),
                "        )",
                "    )",
            ]
        result = "records"
        if self.warehouse:
            result = f'f"Stored {{count:,}} records in {self.table!r}"'
        elif columns:
            names.add("select")
            body += ["", f"rows = select(records, {list(columns)!r})"]
            result = "rows"
        if backend:
            names.add("to_frame")
            body += [f"df = to_frame({result}, {backend!r})"]
            result = "df"
        body += [f"print({result})"]

        header = [
            "# /// script",
            f"# requires-python = {json.dumps(requires_python)}",
            "# dependencies = [",
            *(f"#     {json.dumps(d)}," for d in deps),
            "# ]",
            "# ///",
        ]
        reads_secrets = any("os.environ" in line for line in body + client_args)
        imports = ["import os", ""] if reads_secrets else []
        imports.append(f"from api_explorer import {', '.join(sorted(names))}")
        doc = (
            f'"""Fetch {self.endpoint} from {self.base_url}, '
            'as set up in the API explorer."""'
        )
        return "\n".join([*header, doc, "", *imports, "", *body]) + "\n"

    def _storing_lines(
        self, client_args: list[str], record_args: list[str], names: set[str]
    ) -> list[str]:
        """The script lines that run this plan into its warehouse, as store() does."""
        kind, args, options = self.tracker()
        names.add(type(self.open_warehouse()).__name__)
        clients = ["Client(", *(f"    {a}," for a in client_args), ")"]
        if kind is None:
            opening = ["with " + clients[0], *clients[1:-1], ") as client:"]
        else:
            names.update({"tracked_run", kind.__name__})
            run_args = ", ".join(
                [
                    "shop",
                    repr(self.table),
                    kind.__name__,
                    *(repr(a) for a in args),
                    *(f"{k}={v!r}" for k, v in options.items()),
                ]
            )
            record_args = [*record_args, "incremental=run"]
            opening = [
                "with (",
                *(f"    {line}" for line in clients[:-1]),
                "    ) as client,",
                f"    tracked_run({run_args}) as run,",
                "):",
            ]
        return [
            f"shop = {type(self.open_warehouse()).__name__}({self.warehouse!r})",
            *opening,
            "    count = shop.write(",
            f"        {self.table!r},",
            "        client.records(",
            *(f"            {a}," for a in record_args),
            "        ),",
            *([f"        key={self.key!r},"] if self.key else []),
            *(["        deleted_by=run,"] if kind is Changes else []),
            "    )",
        ]


def package_requirement() -> str:
    """Return how scripts should install this package, matching how it is installed here.

    An editable or local install gives "api-explorer @ file:///...", one from
    git gives "api-explorer @ git+URL@commit", and one from an index gives
    "api-explorer==VERSION". When the package can't be found, it gives the
    bare name.
    """
    try:
        dist = metadata.distribution("api-explorer")
    except metadata.PackageNotFoundError:
        return "api-explorer"
    direct = dist.read_text("direct_url.json")
    if not direct:
        return f"api-explorer=={dist.version}"
    info = json.loads(direct)
    if "vcs_info" in info:
        commit = info["vcs_info"].get("commit_id")
        return f"api-explorer @ git+{info['url']}" + (f"@{commit}" if commit else "")
    return f"api-explorer @ {info['url']}"
