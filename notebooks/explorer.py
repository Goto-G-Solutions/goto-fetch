# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "api-explorer",
#     "marimo>=0.25.1",
# ]
#
# # The package from this repo, installed editable so changes show at once.
# [tool.uv.sources]
# api-explorer = { path = "..", editable = true }
# ///

import marimo

__generated_with = "0.25.1"
app = marimo.App(width="medium")

with app.setup:
    """Explore a paginated JSON API, then copy the fetch as a script."""

    import json
    import logging
    import os
    import time
    from typing import Any

    import marimo as mo

    from api_explorer import (
        FetchPlan,
        logging_to,
        package_requirement,
        select,
    )


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    # API explorer

    Pull records from a paginated JSON API and look through them. Fill in
    the request below, press **Fetch**, then pick columns from the results.
    Secrets come from `$API_TOKEN`, `$API_REFRESH_TOKEN`, `$API_USERNAME`,
    `$API_PASSWORD`, `$API_CLIENT_ID` and `$API_CLIENT_SECRET` when set. They live only in this session, never in
    the file.

    The panel is built on the `api_explorer` package; the examples below
    show the same calls in your own code.
    """)
    return


@app.cell(hide_code=True)
def _():
    # Every input is made here and laid out in the next cell, so the layout
    # can show only what applies without resetting what you typed.
    base_url = mo.ui.text(
        value=os.environ.get("API_BASE_URL", ""),
        placeholder="https://host/api",
        label="Base URL",
        full_width=True,
    )
    endpoint = mo.ui.text(
        placeholder="/products/", label="Endpoint", full_width=True
    )
    trailing_slash = mo.ui.checkbox(value=True, label="Trailing slash")
    method = mo.ui.dropdown(["GET", "POST"], value="GET", label="Method")
    body = mo.ui.text_area(
        placeholder='{"query": "late orders"}',
        label="JSON body",
        rows=3,
        full_width=True,
    )
    read_only = mo.ui.checkbox(label="Safe to retry: it only reads")

    _env_auth = (
        "Client credentials"
        if os.environ.get("API_CLIENT_ID")
        else "Access/refresh"
        if os.environ.get("API_REFRESH_TOKEN") or os.environ.get("API_USERNAME")
        else "Header token"
        if os.environ.get("API_TOKEN")
        else "None"
    )
    auth_kind = mo.ui.dropdown(
        ["None", "Header token", "Access/refresh", "Client credentials"],
        value=_env_auth,
        label="Auth",
    )
    token = mo.ui.text(
        value=os.environ.get("API_TOKEN", ""), kind="password", label="Token"
    )
    scheme = mo.ui.dropdown(
        ["Token", "Bearer", "none"], value="Token", label="Scheme"
    )
    header = mo.ui.text(value="Authorization", label="Header")
    refresh_url = mo.ui.text(value="/token/refresh/", label="Refresh URL")
    refresh_token = mo.ui.text(
        value=os.environ.get("API_REFRESH_TOKEN", ""),
        kind="password",
        label="Refresh token",
    )
    access_token = mo.ui.text(kind="password", label="Access token")
    login_url = mo.ui.text(value="/token/", label="Login URL")
    username = mo.ui.text(
        value=os.environ.get("API_USERNAME", ""), label="Username"
    )
    password = mo.ui.text(
        value=os.environ.get("API_PASSWORD", ""),
        kind="password",
        label="Password",
    )
    token_url = mo.ui.text(value="/oauth/token", label="Token URL")
    client_id = mo.ui.text(
        value=os.environ.get("API_CLIENT_ID", ""), label="Client ID"
    )
    client_secret = mo.ui.text(
        value=os.environ.get("API_CLIENT_SECRET", ""),
        kind="password",
        label="Client secret",
    )
    scope = mo.ui.text(label="Scope (optional)")
    credentials_in = mo.ui.dropdown(
        ["header", "body"], value="header", label="Send the ID and secret in the"
    )

    results_key = mo.ui.text(value="results", label="Results path")
    count_key = mo.ui.text(placeholder="count", label="Count path")
    paging = mo.ui.dropdown(
        ["next link", "limit/offset", "header link", "cursor"],
        value="next link",
        label="Paging",
    )
    next_key = mo.ui.text(value="next", label="Next-page path")
    limit = mo.ui.number(start=1, value=100, step=1, label="Limit")
    cursor_key = mo.ui.text(value="next_cursor", label="Cursor path")
    cursor_param = mo.ui.text(value="cursor", label="Send it as")
    cursor_in = mo.ui.dropdown(["query", "body"], value="query", label="In the")

    max_pages = mo.ui.number(start=0, value=1, step=1, label="Pages (0 = all)")
    timeout = mo.ui.number(start=1, value=20, step=1, label="Timeout (s)")
    verbose = mo.ui.checkbox(label="Verbose log")
    fetch = mo.ui.run_button(label="Fetch", kind="success")

    backend = mo.ui.dropdown(
        ["records", "polars", "pandas", "pyarrow"],
        value="polars",
        label="Script output",
    )
    columns = mo.ui.text_area(
        placeholder="id\nname\nuser.email",
        label="Columns: one path per line; empty shows whole records",
        rows=4,
        full_width=True,
    )
    return (
        access_token,
        auth_kind,
        backend,
        base_url,
        body,
        client_id,
        client_secret,
        columns,
        count_key,
        credentials_in,
        cursor_in,
        cursor_key,
        cursor_param,
        endpoint,
        fetch,
        header,
        limit,
        login_url,
        max_pages,
        method,
        next_key,
        paging,
        password,
        read_only,
        refresh_token,
        refresh_url,
        results_key,
        scheme,
        scope,
        timeout,
        token,
        token_url,
        trailing_slash,
        username,
        verbose,
    )


@app.cell(hide_code=True)
def _(
    access_token,
    auth_kind,
    base_url,
    body,
    client_id,
    client_secret,
    count_key,
    credentials_in,
    cursor_in,
    cursor_key,
    cursor_param,
    endpoint,
    fetch,
    header,
    limit,
    login_url,
    max_pages,
    method,
    next_key,
    paging,
    password,
    read_only,
    refresh_token,
    refresh_url,
    results_key,
    scheme,
    scope,
    timeout,
    token,
    token_url,
    trailing_slash,
    username,
    verbose,
):
    def _section(title: str, *rows: Any) -> Any:
        return mo.vstack([mo.md(f"#### {title}"), *rows], gap=0.5)

    def _row(*items: Any) -> Any:
        return mo.hstack(list(items), justify="start", gap=1, wrap=True)

    def _hint(text: str) -> Any:
        return mo.md(f"<small>{text}</small>")

    if auth_kind.value == "Header token":
        _auth_rows = [
            _row(token, scheme, header),
            _hint(
                "A fixed token. For an API that wants the bare token in a "
                "header of its own, set Scheme to *none* and the Header, "
                "such as `X-API-Key`."
            ),
        ]
    elif auth_kind.value == "Access/refresh":
        _auth_rows = [
            _row(refresh_url, refresh_token, access_token),
            _hint(
                "Paste the tokens you have; the access token is optional. "
                "Or leave them empty and log in below."
            ),
            _row(login_url, username, password),
            _hint(
                "Optional. With a username and password it logs in, and "
                "logs in again whenever the refresh token runs out. URLs "
                "follow the same rules as the endpoint."
            ),
        ]
    elif auth_kind.value == "Client credentials":
        _auth_rows = [
            _row(token_url, client_id, client_secret),
            _row(scope, credentials_in),
            _hint(
                "The OAuth2 client credentials grant, for one service talking "
                "to another. The ID and secret go in a Basic header, which "
                "every OAuth server accepts, or in the form. Tokens renew by "
                "themselves before they expire."
            ),
        ]
    else:
        _auth_rows = [_hint("No authentication.")]

    _post_rows = (
        [
            body,
            _row(read_only),
            _hint(
                "Every page is sent with this body. A POST isn't retried "
                "unless you say it only reads, since retrying could repeat "
                "a change."
            ),
        ]
        if method.value == "POST"
        else []
    )

    if paging.value == "limit/offset":
        _paging_rows = [
            _row(paging, limit),
            _hint(
                "Steps `?limit=&offset=` itself, for pages with no next "
                "link, such as Django Ninja's. The count path lets it stop "
                "exactly at the end."
            ),
        ]
    elif paging.value == "cursor":
        _paging_rows = [
            _row(paging, cursor_key, cursor_param, cursor_in),
            _hint(
                "Sends back the cursor each page gives, in the query string "
                "or the JSON body, until a page gives none."
            ),
        ]
    elif paging.value == "header link":
        _paging_rows = [
            _row(paging),
            _hint(
                "Follows the next-page link in each response's `Link` "
                "header, as GitHub's API gives."
            ),
        ]
    else:
        _paging_rows = [
            _row(paging, next_key),
            _hint(
                "Follows the next-page URL in each page, as Django REST "
                "Framework gives."
            ),
        ]

    mo.vstack(
        [
            _section(
                "Request",
                mo.hstack([base_url, endpoint], widths="equal"),
                _row(method, trailing_slash),
                _hint(
                    'Trailing slash adds a "/" to the endpoint, as Django '
                    "REST Framework expects. Turn it off for routes that "
                    "don't end in one, such as Django Ninja's."
                ),
                *_post_rows,
            ),
            _section("Authentication", _row(auth_kind), *_auth_rows),
            _section(
                "Response",
                _row(results_key, count_key),
                _hint(
                    "Paths into each page: where the records are, and "
                    "(optional) the total count, for progress."
                ),
                *_paging_rows,
            ),
            _section(
                "Run",
                _row(max_pages, timeout, verbose),
                _hint(
                    "Fetch a few pages first to time a full run. Verbose "
                    "logs every request and page below as it happens."
                ),
                fetch,
            ),
        ],
        gap=1.5,
    )
    return


@app.cell(hide_code=True)
def _(
    access_token,
    auth_kind,
    base_url,
    body,
    client_id,
    client_secret,
    count_key,
    credentials_in,
    cursor_in,
    cursor_key,
    cursor_param,
    endpoint,
    fetch,
    header,
    limit,
    login_url,
    max_pages,
    method,
    next_key,
    paging,
    password,
    read_only,
    refresh_token,
    refresh_url,
    results_key,
    scheme,
    scope,
    timeout,
    token,
    token_url,
    trailing_slash,
    username,
    verbose,
):
    mo.stop(
        not fetch.value,
        mo.md("Fill in the request above, then press **Fetch**."),
    )
    _posting = method.value == "POST"
    _body = None
    if _posting and body.value.strip():
        try:
            _body = json.loads(body.value)
        except json.JSONDecodeError as _error:
            raise ValueError(f"The JSON body isn't valid JSON: {_error}") from _error
    plan = FetchPlan(
        base_url=base_url.value,
        endpoint=endpoint.value,
        method=method.value,
        body=_body,
        retry=True if _posting and read_only.value else None,
        trailing_slash=trailing_slash.value,
        timeout=float(timeout.value),
        auth={
            "Header token": "header",
            "Access/refresh": "refresh",
            "Client credentials": "client",
        }.get(auth_kind.value, "none"),
        token=token.value,
        scheme=None if scheme.value == "none" else scheme.value,
        header=header.value,
        refresh_url=refresh_url.value,
        refresh_token=refresh_token.value,
        access_token=access_token.value,
        login_url=token_url.value
        if auth_kind.value == "Client credentials"
        else login_url.value,
        client_id=client_id.value,
        client_secret=client_secret.value,
        scope=scope.value,
        credentials_in=credentials_in.value,
        username=username.value,
        password=password.value,
        results_key=results_key.value or None,
        count_key=count_key.value or None,
        paging={
            "limit/offset": "offset",
            "header link": "header",
            "cursor": "cursor",
        }.get(paging.value, "next"),
        next_key=next_key.value or "next",
        cursor_key=cursor_key.value or "next_cursor",
        cursor_param=cursor_param.value or "cursor",
        cursor_in=cursor_in.value,
        limit=int(limit.value),
        max_pages=int(max_pages.value) or None,
    )

    # The package logs nothing until asked. Here its "page" events drive the
    # spinner, and its lines go to this cell's console: warnings always, and
    # with Verbose every decision (DEBUG) plus httpx2's line per request.
    class _Spinner(logging.Handler):
        def __init__(self, spinner: Any) -> None:
            super().__init__(logging.INFO)
            self.spinner = spinner

        def emit(self, record: logging.LogRecord) -> None:
            if getattr(record, "event", None) == "page":
                self.spinner.update(subtitle=record.getMessage())

    _started = time.monotonic()
    with (
        mo.status.spinner(title="Fetching", subtitle=f"GET {endpoint.value}") as _spin,
        logging_to(_Spinner(_spin), "INFO"),
        logging_to(level="DEBUG" if verbose.value else "WARNING", http=verbose.value),
        plan.client() as _client,
    ):
        fetched = list(plan.records(_client))
    fetch_seconds = time.monotonic() - _started
    return fetch_seconds, fetched, plan


@app.cell(hide_code=True)
def _(backend, columns, fetch_seconds, fetched):
    mo.vstack(
        [
            mo.md(
                f"#### Results\n\n**{len(fetched):,}** records in "
                f"{fetch_seconds:.1f}s"
            ),
            columns,
            mo.hstack(
                [
                    backend,
                    mo.md(
                        "<small>for the **Code** tab: a script that "
                        "repeats this fetch</small>"
                    ),
                ],
                justify="start",
                align="center",
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(backend, columns, fetched, plan):
    _paths = [
        line.strip() for line in columns.value.splitlines() if line.strip()
    ]
    _rows = select(fetched, _paths) if _paths else fetched
    _script = plan.script(
        columns=_paths,
        backend=None if backend.value == "records" else backend.value,
        requirement=package_requirement(),
    )
    _code = mo.vstack(
        [
            mo.md(
                "Save as `fetch.py` and run `uv run fetch.py`; uv installs "
                "what it needs. Set the `$API_*` variables it reads first."
            ),
            mo.ui.code_editor(value=_script, language="python", disabled=True),
        ]
    )
    mo.ui.tabs(
        {
            "Table": mo.ui.table(_rows),
            "JSON": mo.tree(fetched),
            "Code": _code,
        }
    )
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Examples

    Worked examples against an imagined shop API, as you would write them in
    your own notebook or script. Simple filters need no dataframe at all.
    Joins and date maths read better in a dataframe library, here polars,
    but any library `to_frame` supports will do.

    ```python
    import os

    import polars as pl
    from api_explorer import (
        ApiError, Client, Cursor, Offset, PasswordLogin, TokenRefresh,
        explode, select, to_frame, token_headers,
    )

    BASE_URL = "https://api.example.com/api"
    HEADERS = token_headers(os.environ["API_TOKEN"])
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 1: Find products by SKU and update their status

    PATCH isn't retried by default, so a failure is reported, never repeated.

    ```python
    wanted = {f"SKU-{n:04d}" for n in range(1200, 1300)}

    with Client(BASE_URL, headers=HEADERS, timeout=30.0) as client:
        products = client.records("/products/")
        ids = [p["id"] for p in products if p["sku"] in wanted]

        failed = []
        for product_id in ids:
            try:
                client.request(
                    "PATCH",
                    f"/products/{product_id}/",
                    json={"status": "ON_HOLD"},
                )
            except ApiError as error:
                failed.append((product_id, error))

    print(f"{len(ids) - len(failed)} updated, {len(failed)} failed")
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 2: How long each shipment took

    Two calls to the same endpoint, joined on the shipment, then date maths.

    ```python
    keys = ["id", "kind", "at", "shipment.id", "shipment.carrier"]

    with Client(BASE_URL, headers=HEADERS, timeout=30.0) as client:
        def events(kind):
            records = client.records("/shipment-events/", params={"kind": kind})
            return to_frame(select(records, keys), "polars")

        sent, delivered = events("DISPATCHED"), events("DELIVERED")

    as_local = lambda col: pl.col(col).str.to_datetime(time_zone="Europe/London")
    durations = (
        sent.join(delivered, on="shipment.id", how="left", suffix="_delivered")
        .select(
            pl.col("shipment.id").alias("shipment_id"),
            pl.col("shipment.carrier").alias("carrier"),
            as_local("at").alias("sent_at"),
            as_local("at_delivered").alias("delivered_at"),
        )
        .with_columns(took=pl.col("delivered_at") - pl.col("sent_at"))
    )
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 3: Support tickets joined to the orders they mention

    Fallback paths keep working when the API renames a field.

    ```python
    ticket_cols = ["id", "title|subject", "body|description|message", "status|state"]
    order_cols = ["id", "reference|ref", "total|amount", "customer.email"]

    with Client(BASE_URL, headers=HEADERS, timeout=30.0) as client:
        tickets = to_frame(select(client.records("/tickets/"), ticket_cols), "polars")
        orders = to_frame(select(client.records("/orders/"), order_cols), "polars")

    late = (
        tickets.filter(pl.col("title|subject") == "Order arrived late")
        .with_columns(
            order_id=pl.col("body|description|message")
            .str.extract(r"Order #(\d+)", 1)
            .cast(pl.Int64, strict=False)
        )
        .join(orders.rename({"id": "order_id"}), on="order_id")
    )
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 4: One row per order line, from nested lists

    A wildcard column holds a list per record; `explode` turns it into rows,
    stepping through both lists together.

    ```python
    with Client(BASE_URL, headers=HEADERS, timeout=30.0) as client:
        rows = select(
            client.records("/orders/"),
            ["id", "reference", "lines[*].sku", "lines[*].quantity"],
        )

    order_lines = explode(rows, columns=["lines[*].sku", "lines[*].quantity"])
    df = to_frame(order_lines, "polars")
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 5: Pause the listings of products in one category

    One endpoint says which products are in the category; another holds the
    listings to change.

    ```python
    with Client(BASE_URL, headers=HEADERS, timeout=30.0) as client:
        listings = select(client.records("/listings/"), ["id", "sku", "status"])
        products = explode(
            select(client.records("/products/"), ["sku", "categories[*].name"]),
            columns=["categories[*].name"],
        )

        clearance = {
            p["sku"] for p in products if p["categories[*].name"] == "Clearance"
        }
        to_pause = [
            item["id"]
            for item in listings
            if item["status"] == "ACTIVE" and item["sku"] in clearance
        ]

        failed = []
        for listing_id in to_pause:
            try:
                client.request(
                    "PATCH", f"/listings/{listing_id}/", json={"status": "PAUSED"}
                )
            except ApiError as error:
                failed.append((listing_id, error))

    print(f"{len(to_pause) - len(failed)} paused, {len(failed)} failed")
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 6: An API with limit/offset paging and no next link

    Django Ninja's default pages look like `{"items": [...], "count": 250}`.
    `Offset` steps the offset itself. The count is optional: with it the run
    stops exactly at the end; without it, a short or empty page ends it.

    ```python
    with Client(BASE_URL, headers=HEADERS, trailing_slash=False) as client:
        items = list(
            client.records(
                "/items",
                results_key="items",
                count_key="count",
                paging=Offset(limit=100),
            )
        )
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 7: A search API that pages with POST

    The query goes in a JSON body, and each page hands back a cursor that
    goes into the next body. A POST isn't retried by default, since it might
    change data; this one only reads, so it says so.

    ```python
    with Client(BASE_URL, headers=HEADERS) as client:
        hits = list(
            client.records(
                "/search/",
                method="POST",
                json={"query": "late orders", "status": "open"},
                results_key="hits",
                paging=Cursor(send_in="body"),
                retry=True,
            )
        )
    ```
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ### Example 8: Other ways in

    A token in a header of the API's own choosing:

    ```python
    headers = token_headers(os.environ["API_TOKEN"], scheme=None, header="X-API-Key")
    with Client(BASE_URL, headers=headers) as client:
        ...
    ```

    Access and refresh tokens, renewed as they expire. With the login, a
    long job also survives the refresh token running out:

    ```python
    auth = TokenRefresh(
        "/token/refresh/",
        login=PasswordLogin("/token/", os.environ["API_USERNAME"], os.environ["API_PASSWORD"]),
    )
    with Client(BASE_URL, auth=auth) as client:
        for product_id in ids:
            client.request("PATCH", f"/products/{product_id}/", json={"status": "ON_HOLD"})
    ```

    Without a login, start from tokens you were given:
    `TokenRefresh("/token/refresh/", refresh=os.environ["API_REFRESH_TOKEN"])`.

    Login and refresh URLs follow the same rules as endpoints, trailing
    slash included. For django-ninja-jwt they are usually `/token/pair` and
    `/token/refresh`, with the trailing slash turned off.
    """)
    return


@app.cell(hide_code=True)
def _():
    mo.md(r"""
    ## Not built yet

    Ideas parked until this package saves files rather than returning data:

    - **Atomic file writes.** Write to a `.tmp` path and `os.replace()` it on
      success, so a crash can't leave a half-written file.
    - **Resuming a download.** Skipping items already fetched is safe only
      for data that never changes. Most APIs' data does change, so a resume
      would need a staleness check, not just "is it there".
    """)
    return


if __name__ == "__main__":
    app.run()
