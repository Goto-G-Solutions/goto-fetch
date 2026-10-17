"""One fetch, described once: run it, or write it as a standalone script."""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Literal

from .auth import PasswordLogin, TokenRefresh, token_headers
from .client import Client
from .paging import HeaderLink, NextLink, Offset, Paging


@dataclass(frozen=True)
class FetchPlan:
    """One fetch, as the Explore panel describes it.

    client() and records() run it. script() writes it as a PEP 723 script,
    with uv-installable dependencies, that does the same fetch. Secrets
    (token, refresh_token, access_token, password) are used to run, kept
    out of repr, and never written: scripts read $API_TOKEN,
    $API_REFRESH_TOKEN, $API_USERNAME and $API_PASSWORD instead.
    """

    base_url: str
    endpoint: str
    trailing_slash: bool = True
    timeout: float = 20.0
    auth: Literal["none", "header", "refresh"] = "none"
    token: str = field(default="", repr=False)
    scheme: str | None = "Token"
    header: str = "Authorization"
    refresh_url: str = "/token/refresh/"
    refresh_token: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    login_url: str = "/token/"
    username: str = ""
    password: str = field(default="", repr=False)
    results_key: str | None = "results"
    count_key: str | None = None
    paging: Literal["next", "offset", "header"] = "next"
    next_key: str = "next"
    limit: int = 100
    max_pages: int | None = None

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
        return Client(
            self.base_url,
            headers=headers,
            auth=auth,
            timeout=self.timeout,
            trailing_slash=self.trailing_slash,
            **options,
        )

    def paging_strategy(self) -> Paging:
        """The Paging this plan asks for."""
        if self.paging == "offset":
            return Offset(limit=self.limit)
        if self.paging == "header":
            return HeaderLink()
        return NextLink(self.next_key)

    def records(self, client: Client) -> Iterator[Any]:
        """Yield this plan's records through `client`."""
        return client.records(
            self.endpoint,
            results_key=self.results_key,
            count_key=self.count_key,
            paging=self.paging_strategy(),
            max_pages=self.max_pages,
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
        names = {"Client", type(self.paging_strategy()).__name__}
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
        client_args.append(f"timeout={self.timeout!r}")
        if not self.trailing_slash:
            client_args.append("trailing_slash=False")

        record_args = [repr(self.endpoint), f"results_key={self.results_key!r}"]
        if self.count_key:
            record_args.append(f"count_key={self.count_key!r}")
        if self.paging == "offset":
            record_args.append(f"paging=Offset(limit={self.limit!r})")
        elif self.paging == "header":
            record_args.append("paging=HeaderLink()")
        elif self.next_key != "next":
            record_args.append(f"paging=NextLink({self.next_key!r})")
        if self.max_pages:
            record_args.append(f"max_pages={self.max_pages!r}")

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
        if columns:
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
