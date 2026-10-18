"""Calls: one description of a call, made now or written as Python source.

FetchPlan's own tests check that a plan and its script do the same fetch;
these cover the parts that make that possible: building, writing,
secrets, script-only names, and the names a script imports.
"""

import pytest

from api_explorer.calls import Call, Name, Secret, build, code, names, reads_environment
from api_explorer.plan import _with_extra


def pair(a, b=0, *, c=None):
    return (a, b, c)


def test_a_call_builds_with_its_values_and_secrets_and_extra_arguments():
    call = Call(pair, (Secret("API_TOKEN", "t0k"),), {"c": Call(pair, (1,))})
    assert call.build(b=2) == ("t0k", 2, (1, 0, None))


def test_a_call_is_written_with_secrets_read_from_the_environment():
    call = Call(
        pair, (Secret("API_TOKEN", "t0k"),), {"c": Secret("X", "", optional=True)}
    )
    assert call.code() == "pair(os.environ['API_TOKEN'], c=os.environ.get('X'))"
    assert "t0k" not in call.code()


def test_an_optional_secret_that_is_empty_builds_as_none():
    assert build(Secret("X", "", optional=True)) is None
    assert build(Secret("X", "", optional=False)) == ""


def test_a_long_call_is_written_one_argument_a_line():
    call = Call(pair, ("a" * 50, "b" * 50))
    assert (
        call.code(1)
        == "pair(\n        '" + "a" * 50 + "',\n        '" + "b" * 50 + "',\n    )"
    )


def test_classes_and_functions_are_written_by_name_and_imported():
    call = Call("client.records", (Name("run"),), {"paging": Call(pair, (dict,))})
    assert code(call) == "client.records(run, paging=pair(dict))"
    assert names(call) == {"pair", "dict"}


def test_a_name_or_a_method_text_can_only_be_written():
    with pytest.raises(TypeError, match="only exists in a script"):
        build(Name("client"))
    with pytest.raises(TypeError, match="can only be written"):
        Call("client.records").build()


def test_a_list_is_written_item_by_item():
    # Regression: a list was written with repr, so a Secret inside one
    # was written as Secret(...) rather than as a read of the environment.
    call = Call(pair, ([Secret("X", "x"), Call(pair, (1,)), "a"],))
    assert code(call) == "pair([os.environ['X'], pair(1), 'a'])"
    assert build(call) == (["x", (1, 0, None), "a"], 0, None)


def test_reads_environment_finds_a_secret_at_any_depth():
    assert reads_environment(
        Call(pair, kwargs={"c": [Call(pair, (Secret("X", "x"),))]})
    )
    assert not reads_environment(Call(pair, (1,)))


@pytest.mark.parametrize(
    ("requirement", "with_duckdb"),
    [
        ("api-explorer", "api-explorer[duckdb]"),
        ("api-explorer==0.1.0", "api-explorer[duckdb]==0.1.0"),
        ("api-explorer @ file:///src/x", "api-explorer[duckdb] @ file:///src/x"),
        ("api-explorer[fast]==0.1.0", "api-explorer[fast,duckdb]==0.1.0"),
        # Regression: another package starting with this one's name got
        # the extra too.
        ("api-explorer-tools==1", "api-explorer-tools==1"),
    ],
    ids=["bare", "pinned", "local", "other-extra", "other-package"],
)
def test_a_script_needing_duckdb_asks_for_this_package_s_extra(
    requirement, with_duckdb
):
    # One home for the version floor: the extra, not a second "duckdb" line.
    assert _with_extra(requirement, "duckdb") == with_duckdb
