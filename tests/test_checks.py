"""Response checks: errors an API reports inside a successful response.

Some APIs answer 200 and put the error in the body. Without a check, that
body passes for data: a page of records ends early, or reads as empty, and
nothing says so. These tests cover error_at's verdicts, and what the
client does with a problem a check finds: a permanent one fails at once, a
temporary one goes to the retry policy, and records stops instead of
loading part of the data.
"""

import json

import pytest

from goto_fetch import ApiError, Problem, Retry, error_at, http
from goto_fetch.testing import FakeApi, replies


def ok(body) -> http.Response:
    return http.Response(200, json=body)


@pytest.mark.parametrize(
    "body",
    [
        {"error": "quota exceeded"},
        {"errors": [{"message": "bad field"}]},
        {"error": {"code": 7}},
    ],
    ids=["error-text", "errors-list", "error-object"],
)
def test_error_at_finds_an_error_by_default_path(body):
    problem = error_at()(ok(body))
    assert isinstance(problem, Problem)
    assert problem.temporary is False


@pytest.mark.parametrize(
    "response",
    [
        ok({"results": [1]}),
        ok({"error": None, "results": [1]}),
        ok({"error": "", "errors": []}),
        ok({"errors": {}}),
        ok([1, 2]),
        http.Response(200, text="not json"),
        http.Response(204),
    ],
    ids=[
        "no-key",
        "null",
        "empty-text-and-list",
        "empty-object",
        "list-body",
        "text",
        "no-body",
    ],
)
def test_error_at_passes_a_body_without_an_error(response):
    assert error_at()(response) is None


def test_error_at_names_the_path_and_what_it_found():
    problem = error_at("meta.status_error", temporary=True)(
        ok({"meta": {"status_error": "busy"}})
    )
    assert problem == Problem("'meta.status_error' holds \"busy\"", temporary=True)


def test_error_at_keeps_a_long_error_short():
    problem = error_at()(ok({"error": "x" * 1000}))
    assert len(problem.message) < 250


def test_a_permanent_problem_fails_at_once_with_status_200():
    api = replies(ok({"error": "bad token"}), ok({"results": []}))
    client = api.client(check=error_at())
    with pytest.raises(ApiError) as raised:
        client.request("GET", "/x/")
    assert raised.value.status == 200
    assert "returned HTTP 200 but reports a problem" in str(raised.value)
    assert "bad token" in str(raised.value)
    assert len(api.calls) == 1


def test_a_temporary_problem_is_retried_until_it_goes_away():
    api = replies(ok({"error": "busy"}), ok({"error": "busy"}), ok({"ok": True}))
    client = api.client(check=error_at(temporary=True))
    assert client.get_json("/x/") == {"ok": True}
    assert len(api.calls) == 3


def test_a_temporary_problem_that_stays_fails_after_the_last_try():
    api = FakeApi(lambda request: ok({"error": "busy"}))
    client = api.client(
        check=error_at(temporary=True), retry=Retry(attempts=2, backoff=0)
    )
    with pytest.raises(ApiError, match="busy"):
        client.request("GET", "/x/")
    assert len(api.calls) == 2


def test_a_temporary_problem_on_a_post_is_not_retried():
    # The same rule as a 503: repeating a POST can repeat what it did.
    api = replies(ok({"error": "busy"}), ok({"ok": True}))
    client = api.client(check=error_at(temporary=True))
    with pytest.raises(ApiError):
        client.request("POST", "/x/", json={})
    assert len(api.calls) == 1


def test_a_check_of_your_own_may_return_plain_text():
    def check(response):
        return None if response.json().get("status") == "ok" else "status isn't ok"

    client = replies(ok({"status": "failed"})).client(check=check)
    with pytest.raises(ApiError, match="status isn't ok"):
        client.request("GET", "/x/")


def test_without_a_check_an_error_body_passes_for_data():
    # The default stays off: a 200 is trusted unless a check says otherwise.
    assert replies(ok({"error": "x"})).client().get_json("/x/") == {"error": "x"}


PAGES = {
    "1": {"results": [1, 2], "next": "?page=2"},
    # An error page that still looks like a page: empty results, no next.
    "2": {"results": [], "next": None, "error": "rate limited, try later"},
}


def paged_api_with_an_error_page() -> FakeApi:
    return FakeApi(lambda request: ok(PAGES[request.url.params.get("page", "1")]))


def test_without_a_check_an_error_page_reads_as_the_last_page():
    # The silent loss the check exists for, kept as a test so the reason
    # stays visible: two records come back, and nothing says why.
    assert list(paged_api_with_an_error_page().client().records("/items/")) == [1, 2]


def test_records_stops_at_a_page_reporting_an_error_instead_of_loading_part():
    seen = []
    client = paged_api_with_an_error_page().client(check=error_at())
    with pytest.raises(ApiError, match="rate limited"):
        seen.extend(client.records("/items/"))
    assert seen == [1, 2]


@pytest.mark.parametrize("value", [False, 0, 0.0], ids=["false", "zero", "zero-float"])
def test_error_at_reads_false_and_zero_as_no_error(value):
    # Regression: {"error": false}, which many APIs send on success, failed
    # every request.
    assert error_at()(ok({"error": value, "results": []})) is None


@pytest.mark.parametrize("value", [True, 1, "0"], ids=["true", "one", "zero-as-text"])
def test_error_at_still_reports_true_one_and_text(value):
    found = error_at()(ok({"error": value}))
    assert found == Problem(f"'error|errors' holds {json.dumps(value)}")


def test_error_at_parses_with_the_decoder_it_is_given():
    seen = []

    def decode(content):
        seen.append(content)
        return {"error": "from my decoder"}

    problem = error_at(decode=decode)(http.Response(200, content=b"{}"))
    assert seen == [b"{}"]
    assert "from my decoder" in problem.message
