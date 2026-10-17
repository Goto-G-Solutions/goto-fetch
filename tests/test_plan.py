"""FetchPlan and package_requirement: running a fetch, and writing it as a script."""

import functools
import json
import sys
import types

import httpx2
import pytest
from support import auth_server

import api_explorer
from api_explorer import Client, FetchPlan, Retry, package_requirement, select, to_frame
from api_explorer import plan as plan_module

SECRETS = {
    "token": "tok-secret",
    "refresh_token": "refresh-secret",
    "access_token": "access-secret",
    "password": "pw-secret",
}


def keyed_offset_api(key: str):
    """A Ninja-style endpoint over records 0..119 that wants X-API-Key: key."""

    def handler(request):
        if request.headers.get("X-API-Key") != key:
            return httpx2.Response(401)
        query = dict(request.url.params)
        start, size = int(query["offset"]), int(query["limit"])
        items = [{"id": i, "user": {"name": f"u{i}"}} for i in range(120)]
        return httpx2.Response(
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
        fake.Client = functools.partial(
            Client, transport=httpx2.MockTransport(handler), retry=Retry(backoff=0.0)
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


@pytest.mark.parametrize("auth", ["header", "refresh"])
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
        KEYED,
    ],
    ids=["no-auth", "refresh-token", "login", "custom-next", "header-offset"],
)
def test_every_kind_of_plan_writes_valid_python(plan):
    compile(plan.script(columns=["id"], backend="polars"), "fetch.py", "exec")


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
    with KEYED.client(transport=httpx2.MockTransport(handler)) as client:
        expected = select(KEYED.records(client), columns)
    namespace = run_script(KEYED.script(columns=columns), handler, API_TOKEN="k-123")
    assert namespace["rows"] == expected
    assert len(expected) == 120


def test_a_login_script_logs_in_from_environment_variables(run_script):
    handler, state = auth_server()
    plan = FetchPlan(
        "http://api.test/api", "/items/", auth="refresh", username="me", password="pw"
    )
    namespace = run_script(plan.script(), handler, API_USERNAME="me", API_PASSWORD="pw")
    assert namespace["records"] == [{"id": 1}]
    assert [path for _, path, _ in state["calls"]] == ["/api/token/", "/api/items/"]


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
