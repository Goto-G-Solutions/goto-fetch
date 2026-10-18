"""FetchPlan and package_requirement: running a fetch, storing it, writing it as a script.

The plan and its script must do the same fetch, so several tests run the
script itself. Also here: store() into a warehouse, tracked or not, and
the plans refused because they could lose records. api_explorer.calls,
which keeps the two in step, has its own tests in test_calls.
"""

import functools
import json
import sys
import types

import pytest

import api_explorer
from api_explorer import (
    ApiError,
    Client,
    FetchPlan,
    Keyset,
    Retry,
    SqliteWarehouse,
    http,
    package_requirement,
    select,
    to_frame,
)
from api_explorer import plan as plan_module
from api_explorer.testing import (
    FakeApi,
    MemoryState,
    MemoryWarehouse,
    TokenServer,
    changes_api,
    keyset_api,
    next_link_api,
)

SECRETS = {
    "token": "tok-secret",
    "refresh_token": "refresh-secret",
    "access_token": "access-secret",
    "password": "pw-secret",
    "client_secret": "client-secret",
}


def keyed_offset_api(key: str):
    """A Ninja-style endpoint over records 0..119 that wants X-API-Key: key."""

    def handler(request):
        if request.headers.get("X-API-Key") != key:
            return http.Response(401)
        query = dict(request.url.params)
        start, size = int(query["offset"]), int(query["limit"])
        items = [{"id": i, "user": {"name": f"u{i}"}} for i in range(120)]
        return http.Response(
            200, json={"items": items[start : start + size], "count": 120}
        )

    return handler


@pytest.fixture
def run_script(monkeypatch, capsys):
    """Run a generated script against a handler, and return its globals.

    The script's `from api_explorer import ...` gets this package, with a
    Client whose requests go to the handler.
    """

    def run(text: str, handler, **env: str) -> dict:
        fake = types.ModuleType("api_explorer")
        fake.__dict__.update(vars(api_explorer))
        transport = handler if isinstance(handler, FakeApi) else FakeApi(handler)
        fake.Client = functools.partial(
            Client, transport=transport, retry=Retry(backoff=0.0)
        )
        monkeypatch.setitem(sys.modules, "api_explorer", fake)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        namespace: dict = {"__name__": "__main__"}
        # Running the generated script is what these tests check.
        exec(compile(text, "fetch.py", "exec"), namespace)  # noqa: S102
        capsys.readouterr()
        return namespace

    return run


KEYED = FetchPlan(
    "http://api.test/api",
    "/items",
    trailing_slash=False,
    auth="header",
    token="k-123",
    scheme=None,
    header="X-API-Key",
    results_key="items",
    count_key="count",
    paging="offset",
    limit=50,
)


SEARCH = FetchPlan(
    "http://api.test/api",
    "/search/",
    method="POST",
    body={"query": "late", "filters": {"open": True}},
    retry=True,
    results_key="hits",
    paging="cursor",
    cursor_in="body",
)


@pytest.mark.parametrize("auth", ["header", "refresh", "client"])
def test_scripts_and_repr_never_contain_secrets(auth):
    plan = FetchPlan("http://h/api", "/a/", auth=auth, username="me", **SECRETS)
    for text in (plan.script(), repr(plan)):
        for secret in SECRETS.values():
            assert secret not in text


@pytest.mark.parametrize(
    "plan",
    [
        FetchPlan("http://h/api", "/a/"),
        FetchPlan("http://h/api", "/a/", auth="refresh", refresh_token="r"),
        FetchPlan("http://h/api", "/a/", auth="refresh", username="u", password="p"),
        FetchPlan(
            "http://h/api", "/a/", results_key=None, next_key="links.next", max_pages=3
        ),
        FetchPlan("http://h/api", "/a/", results_key=None, paging="header"),
        SEARCH,
        FetchPlan("http://h/api", "/a/", auth="client", client_id="c", scope="read"),
        KEYED,
        FetchPlan("http://h/api", "/a/", error_path="meta.error"),
    ],
    ids=[
        "no-auth",
        "refresh-token",
        "login",
        "custom-next",
        "header-link",
        "post-cursor",
        "client-credentials",
        "header-offset",
        "error-path",
    ],
)
def test_every_kind_of_plan_writes_valid_python(plan):
    compile(plan.script(columns=["id"], backend="polars"), "fetch.py", "exec")


def search_api(request):
    """A POST search: the body's "cursor" picks one of two pages."""
    cursor = json.loads(request.content).get("cursor")
    pages = {
        None: {"hits": [1, 2], "next_cursor": "c2"},
        "c2": {"hits": [3], "next_cursor": None},
    }
    return http.Response(200, json=pages[cursor])


def test_a_post_search_script_fetches_what_its_plan_fetches(run_script):
    # A second end-to-end script test, on purpose: method, body, retry and
    # cursor paging are written by their own branches of script().
    with SEARCH.client(transport=FakeApi(search_api)) as client:
        expected = list(SEARCH.records(client))
    namespace = run_script(SEARCH.script(), search_api)
    assert namespace["records"] == expected == [1, 2, 3]


def test_the_script_header_lists_the_package_and_the_backend():
    text = FetchPlan("http://h/api", "/a/").script(
        backend="polars", requirement="api-explorer @ file:///x"
    )
    assert '#     "api-explorer @ file:///x",\n#     "polars",' in text
    assert '# requires-python = ">=3.11"' in text


def test_a_script_imports_os_only_when_it_reads_secrets():
    assert "import os" not in FetchPlan("http://h/api", "/a/").script()
    assert "import os" in KEYED.script()


def test_a_script_fetches_what_its_plan_fetches(run_script):
    handler = keyed_offset_api("k-123")
    columns = ["id", "user.name"]
    with KEYED.client(transport=FakeApi(handler)) as client:
        expected = select(KEYED.records(client), columns)
    namespace = run_script(KEYED.script(columns=columns), handler, API_TOKEN="k-123")
    assert namespace["rows"] == expected
    assert len(expected) == 120


def test_a_login_script_logs_in_from_environment_variables(run_script):
    server = TokenServer()
    plan = FetchPlan(
        "http://api.test/api", "/items/", auth="refresh", username="me", password="pw"
    )
    namespace = run_script(plan.script(), server, API_USERNAME="me", API_PASSWORD="pw")
    assert namespace["records"] == [{"id": 1}]
    assert server.paths == ["/api/token/", "/api/items/"]


def test_a_script_with_a_backend_builds_that_library_s_frame(run_script, backend):
    namespace = run_script(
        KEYED.script(columns=["id"], backend=backend),
        keyed_offset_api("k-123"),
        API_TOKEN="k-123",
    )
    assert type(namespace["df"]) is type(to_frame([], backend))
    assert len(namespace["df"]) == 120


class FakeDistribution:
    """Just enough of importlib.metadata.Distribution for package_requirement."""

    def __init__(self, version: str, direct_url: dict | None):
        self.version = version
        self._direct = None if direct_url is None else json.dumps(direct_url)

    def read_text(self, name: str) -> str | None:
        return self._direct if name == "direct_url.json" else None


@pytest.mark.parametrize(
    ("direct_url", "requirement"),
    [
        (None, "api-explorer==0.1.0"),
        (
            {"url": "file:///src/api-explorer", "dir_info": {"editable": True}},
            "api-explorer @ file:///src/api-explorer",
        ),
        (
            {
                "url": "https://github.com/me/api-explorer",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
            },
            "api-explorer @ git+https://github.com/me/api-explorer@abc123",
        ),
    ],
    ids=["from-an-index", "local-or-editable", "from-git"],
)
def test_package_requirement_matches_how_the_package_is_installed(
    monkeypatch, direct_url, requirement
):
    monkeypatch.setattr(
        plan_module.metadata,
        "distribution",
        lambda name: FakeDistribution("0.1.0", direct_url),
    )
    assert package_requirement() == requirement


def test_package_requirement_falls_back_to_the_bare_name(monkeypatch):
    def missing(name):
        raise plan_module.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(plan_module.metadata, "distribution", missing)
    assert package_requirement() == "api-explorer"


def test_a_client_credentials_script_gets_its_token_from_the_environment(run_script):
    # Its own script test: the client-credentials branch of script() writes
    # its own auth lines.
    server = TokenServer()
    plan = FetchPlan(
        "http://api.test/api",
        "/items/",
        auth="client",
        login_url="/token/",
        client_id="client",
        client_secret="secret",
    )
    with plan.client(transport=FakeApi(server)) as client:
        expected = list(plan.records(client))
    namespace = run_script(plan.script(), server, API_CLIENT_SECRET="secret")
    assert namespace["records"] == expected == [{"id": 1}]


def test_an_error_path_stops_the_plan_and_its_script_alike(run_script):
    # Its own test: error_path adds a check to the client and a line to
    # the script, and both must refuse a 200 that reports an error.
    def handler(request):
        return http.Response(200, json={"error": "quota exceeded"})

    plan = FetchPlan("http://api.test/api", "/items/", error_path="error")
    with (
        plan.client(transport=FakeApi(handler)) as client,
        pytest.raises(ApiError, match="quota exceeded"),
    ):
        list(plan.records(client))
    assert "check=error_at('error')" in plan.script()  # and the script runs, below
    with pytest.raises(ApiError, match="quota exceeded"):
        run_script(plan.script(), handler)


def test_a_plan_without_an_error_path_has_no_check():
    plan = FetchPlan("http://h/api", "/a/")
    assert "error_at" not in plan.script()
    with plan.client() as client:
        assert client.check is None


def test_a_plan_that_stores_nothing_imports_no_paging_it_doesnt_use():
    assert "NextLink" not in FetchPlan("http://h/api", "/a/").script()


# Storing


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"track": "incremental"}, "needs a table"),
        ({"warehouse": "x.duckdb"}, "needs a table"),
        ({"track": "changes", "warehouse": "x.duckdb", "table": "t"}, "needs a key"),
        (
            {
                "track": "incremental",
                "warehouse": "x.duckdb",
                "table": "t",
                "max_pages": 1,
            },
            "fetches every page",
        ),
        ({"paging": "keyset"}, "needs change_param"),
    ],
    ids=[
        "track-without-table",
        "no-table",
        "changes-without-key",
        "max-pages",
        "keyset",
    ],
)
def test_a_plan_that_could_lose_records_is_refused(options, message):
    with pytest.raises(ValueError, match=message):
        FetchPlan("http://h/api", "/a/", **options)


def test_keyset_paging_pages_by_the_change_field():
    plan = FetchPlan(
        "http://h/api", "/a/", paging="keyset", change_param="since", limit=50
    )
    assert plan.paging_strategy() == Keyset("updated_at", "since", 50)


def stamped(id_: int, day: int) -> dict:
    return {"id": id_, "updated_at": f"2026-01-{day:02d}T00:00:00Z"}


def stored(path) -> list[tuple]:
    duckdb = pytest.importorskip("duckdb")
    with duckdb.connect(str(path)) as connection:
        return connection.sql(
            "SELECT id, updated_at FROM orders ORDER BY id"
        ).fetchall()


@pytest.mark.parametrize("track", ["none", "incremental", "changes"])
def test_store_keeps_the_warehouse_equal_to_the_source_run_after_run(tmp_path, track):
    pytest.importorskip("duckdb")
    data = [stamped(1, 1), stamped(2, 2)]
    api = changes_api(data)
    plan = FetchPlan(
        "http://api.test/api",
        "/orders/",
        warehouse=str(tmp_path / "shop.duckdb"),
        table="orders",
        key="id",
        track=track,
        change_param="updated_since",
    )
    first = plan.store(api.client())
    data[0] = stamped(1, 3)
    data.append(stamped(3, 4))
    if track == "changes":
        del data[1]
    second = plan.store(api.client())
    assert stored(tmp_path / "shop.duckdb") == [
        (r["id"], r["updated_at"]) for r in sorted(data, key=lambda r: r["id"])
    ]
    # Only a tracked plan fetches less the second time.
    assert (first, second) == {
        "none": (2, 3),
        "incremental": (2, 2),
        "changes": (2, 2),
    }[track]


def test_a_stored_plan_s_script_stores_the_same_way(tmp_path, run_script):
    pytest.importorskip("duckdb")
    data = [stamped(1, 1), stamped(2, 2)]
    api = changes_api(data)
    plan = FetchPlan(
        "http://api.test/api",
        "/orders/",
        warehouse=str(tmp_path / "shop.duckdb"),
        table="orders",
        track="incremental",
        change_param="updated_since",
        paging="keyset",
        limit=2,
    )
    text = plan.script()
    assert "tracked_run(shop, 'orders', Incremental" in text
    assert '"api-explorer[duckdb]"' in text
    assert run_script(text, keyset_api(data))["count"] == 2
    data.append(stamped(3, 3))
    assert run_script(text, keyset_api(data))["count"] == 1
    assert [row[0] for row in stored(tmp_path / "shop.duckdb")] == [1, 2, 3]
    assert api.calls == []


def tracked(**options) -> FetchPlan:
    return FetchPlan(
        "http://api.test/api",
        "/orders/",
        table="orders",
        key="id",
        track="incremental",
        change_param="updated_since",
        **options,
    )


def test_a_tracked_plan_with_nowhere_to_store_is_refused_when_run_or_written():
    plan = tracked()
    with pytest.raises(ValueError, match="names no warehouse"):
        plan.store(changes_api([]).client())
    with pytest.raises(ValueError, match="needs a warehouse"):
        plan.script()


def test_store_takes_a_warehouse_of_your_own():
    shop = MemoryWarehouse()
    data = [stamped(1, 1), stamped(2, 2)]
    assert tracked().store(changes_api(data).client(), warehouse=shop) == 2
    data.append(stamped(3, 3))
    assert tracked().store(changes_api(data).client(), warehouse=shop) == 1
    assert sorted(r["id"] for r in shop.read("orders")) == [1, 2, 3]


def test_store_needs_somewhere_to_keep_a_tracked_run_s_state():
    class NoState:
        def write(self, table, records, *, key=None, deleted_by=None):
            return len(list(records))

        def read(self, table, limit=None):
            return []

    with pytest.raises(TypeError, match="pass state="):
        tracked().store(changes_api([]).client(), warehouse=NoState())
    state = MemoryState()
    tracked().store(
        changes_api([stamped(1, 1)]).client(), warehouse=NoState(), state=state
    )
    assert state.get("orders")["last"] == stamped(1, 1)["updated_at"]


def test_an_sqlite_plan_stores_and_writes_a_script_for_sqlite(tmp_path, run_script):
    plan = tracked(warehouse=str(tmp_path / "shop.db"), warehouse_kind="sqlite")
    data = [stamped(1, 1)]
    assert plan.store(changes_api(data).client()) == 1
    text = plan.script()
    assert "shop = SqliteWarehouse(" in text
    assert '"duckdb"' not in text
    data.append(stamped(2, 2))
    assert run_script(text, changes_api(data))["count"] == 1
    stored_ids = [r["id"] for r in SqliteWarehouse(tmp_path / "shop.db").read("orders")]
    assert sorted(stored_ids) == [1, 2]


@pytest.mark.parametrize("table", ['my "orders"', "{orders}"], ids=["quote", "braces"])
def test_a_script_runs_whatever_the_table_is_called(tmp_path, run_script, table):
    # Regression: the table name went inside an f-string, where a quote is
    # a SyntaxError on Python 3.11 and braces break it.
    plan = FetchPlan(
        "http://api.test/api",
        "/orders/",
        warehouse=str(tmp_path / "shop.db"),
        warehouse_kind="sqlite",
        table=table,
    )
    namespace = run_script(plan.script(), changes_api([stamped(1, 1)]))
    assert namespace["count"] == 1


def test_a_script_s_docstring_hides_secrets_in_its_urls():
    plan = FetchPlan("http://h/api?api_key=S3", "/a/?token=T9")
    first_lines = plan.script().split("from api_explorer")[0]
    assert "S3" not in first_lines.split("dependencies")[1]
    assert "T9" not in first_lines.split("dependencies")[1]


def test_an_access_token_is_read_from_the_environment_only_if_set():
    plan = FetchPlan(
        "http://h/api", "/a/", auth="refresh", refresh_token="r", access_token="a"
    )
    text = plan.script()
    assert "access=os.environ.get('API_ACCESS_TOKEN')" in text
    assert "refresh=os.environ['API_REFRESH_TOKEN']" in text


def test_a_rate_limit_reaches_the_plan_s_client_and_its_script():
    plan = FetchPlan("http://api.test/api", "/items/", rate_limit=5)
    with plan.client(transport=next_link_api(3)) as client:
        assert client.rate_limit.per_second == 5
        assert list(plan.records(client)) == [0, 1, 2]
    text = plan.script()
    assert "rate_limit=RateLimit(5)" in text
    compile(text, "fetch.py", "exec")


def test_options_given_to_a_plan_s_client_go_over_its_own():
    # Regression: any option the plan also set raised "multiple values".
    plan = FetchPlan("http://api.test/api", "/items/", timeout=20)
    with plan.client(timeout=5, transport=next_link_api(1)) as client:
        assert client.timeout == http.Timeout(5, 5)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"rate_limit": -1}, "rate_limit can't be negative"),
        ({"timeout": http.Timeout(1, 2)}, "number of seconds"),
    ],
    ids=["negative-rate", "timeout-object"],
)
def test_a_plan_refuses_settings_its_script_couldn_t_write(options, message):
    with pytest.raises((ValueError, TypeError), match=message):
        FetchPlan("http://h/api", "/a/", **options)


def test_an_incremental_plan_without_a_filter_is_written_without_one():
    # The script read Incremental('updated_at', None), a None with no meaning.
    plan = FetchPlan("http://api.test/api", "/x/", track="incremental", table="t")
    assert plan.tracker_call().code() == "Incremental('updated_at')"
