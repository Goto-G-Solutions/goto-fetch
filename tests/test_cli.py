"""The gtfetch command: opening the explorer, and printing a guide."""

import importlib.util
import sys

import pytest

from goto_fetch import explorer
from goto_fetch.cli import main
from goto_fetch.docs import pages


# test_docs checks the same pages through `python -m goto_fetch.docs`; these
# two check the gtfetch command hands its arguments on to it.
def test_docs_prints_a_page_and_the_index(capsys):
    assert main(["docs", "paging"]) == 0
    assert capsys.readouterr().out.startswith("# Paging")
    assert main(["docs"]) == 0
    assert capsys.readouterr().out.startswith("# goto-fetch guides")


def test_docs_refuses_a_page_it_doesnt_have(capsys):
    assert main(["docs", "nope"]) == 2
    assert ", ".join(pages()) in capsys.readouterr().err


@pytest.fixture
def ran(monkeypatch):
    """The commands the explorer would have run, instead of running marimo.

    marimo counts as installed, whether it is or not: the core-only test
    run doesn't have it, and these tests are about the command, not marimo.
    """
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: True if name == "marimo" else real(name),
    )
    commands = []
    monkeypatch.setattr(
        explorer.subprocess, "call", lambda cmd: commands.append(cmd) or 0
    )
    return commands


def test_explore_copies_the_notebook_here_and_opens_it(
    tmp_path, monkeypatch, capsys, ran
):
    monkeypatch.chdir(tmp_path)
    assert main(["explore"]) == 0
    assert (tmp_path / "explorer.py").read_text() == explorer.NOTEBOOK.read_text()
    assert "Wrote explorer.py" in capsys.readouterr().out
    assert ran[-1] == [
        sys.executable,
        "-m",
        "marimo",
        "edit",
        "--no-sandbox",
        "explorer.py",
    ]


def test_explore_leaves_an_existing_notebook_as_it_is(tmp_path, capsys, ran):
    mine = tmp_path / "mine.py"
    mine.write_text("# my edits")
    assert main(["explore", str(mine)]) == 0
    assert mine.read_text() == "# my edits"
    assert "Wrote" not in capsys.readouterr().out


def test_explore_without_marimo_names_the_extra(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "marimo" else real(name),
    )
    assert main(["explore"]) == 2
    assert "goto-fetch[explorer]" in capsys.readouterr().err
    # Regression: the notebook was written first, so a run that couldn't
    # open it still left explorer.py behind and said "Wrote".
    assert list(tmp_path.iterdir()) == []


def test_the_shipped_notebook_runs_on_its_own():
    # Guard: a `[tool.uv.sources]` block pinning the package to a checkout
    # would break `marimo edit --sandbox` on a copy outside the repository.
    text = explorer.NOTEBOOK.read_text()
    assert text.startswith("# /// script")
    assert "tool.uv.sources" not in text


def test_help_exits_cleanly():
    with pytest.raises(SystemExit) as left:
        main(["explore", "--help"])
    assert left.value.code == 0


def test_explore_says_why_it_cant_write_the_notebook(tmp_path, capsys, ran):
    # Regression: a folder that isn't there gave a bare traceback.
    assert main(["explore", str(tmp_path / "missing" / "explorer.py")]) == 2
    assert "Can't write" in capsys.readouterr().err
    assert ran == []


def test_explore_doesnt_write_through_a_dangling_link(tmp_path, ran):
    # Regression: exists() is false for a link that points nowhere, so the
    # notebook was written to wherever the link led.
    target = tmp_path / "elsewhere.py"
    link = tmp_path / "explorer.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this system doesn't let the test make a link")
    assert main(["explore", str(link)]) == 0
    assert not target.exists()
