"""The guides: every example runs, every page is listed, and they can be read from a shell.

A page's ```python blocks run in order, sharing one namespace, in a
fresh folder, so a page reads as one session. A block fenced
```python skip shows a call against a real API: it is only compiled. A
page needing an optional package names it on its first line, and is
skipped without it.
README.md's blocks are only compiled, since they sketch code around an
API of your own. This is what keeps the guides from going stale.
"""

import re
from pathlib import Path

import pytest

from api_explorer import docs
from api_explorer.docs.__main__ import main

ROOT = Path(__file__).parent.parent
FENCE = re.compile(r"^```python( skip)?\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def blocks(text: str) -> list[tuple[bool, str]]:
    """Each python block of `text`: whether it runs, and its code."""
    return [(not skip, code) for skip, code in FENCE.findall(text)]


GUIDES = [page for page in docs.pages() if page != docs.INDEX]


@pytest.mark.parametrize("page", [*docs.pages(), "skill"])
def test_every_example_in_a_guide_runs(page, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = docs.read(page)
    # A page that needs an optional package says so on its first line:
    # <!-- needs: duckdb, polars -->
    needs = re.match(r"<!-- needs: ([\w, ]+) -->", text)
    for module in needs[1].split(",") if needs else []:
        pytest.importorskip(module.strip())
    namespace: dict = {"__name__": "__docs__"}
    for number, (runs, code) in enumerate(blocks(text), start=1):
        compiled = compile(code, f"{page}.md, block {number}", "exec")
        if runs:
            exec(compiled, namespace)  # noqa: S102 (running the guide is the test)


def test_every_example_in_the_readme_compiles():
    for number, (_, code) in enumerate(blocks((ROOT / "README.md").read_text()), 1):
        compile(code, f"README.md, block {number}", "exec")


def test_the_index_and_llms_txt_list_every_guide():
    index = docs.read()
    llms = (ROOT / "llms.txt").read_text()
    for page in GUIDES:
        assert f"({page}.md)" in index, page
        assert f"src/api_explorer/docs/{page}.md" in llms, page


def test_a_guide_prints_from_a_shell(capsys):
    assert main(["paging"]) == 0
    assert capsys.readouterr().out.startswith("# Paging")
    assert main(["--path"]) == 0
    assert capsys.readouterr().out.strip() == str(docs.path())


def test_an_unknown_page_lists_the_pages(capsys):
    assert main(["nope"]) == 2
    assert "the pages are: README" in capsys.readouterr().err


def test_the_skill_names_itself_and_says_when_to_use_it():
    skill = docs.read("skill")
    front = skill.split("---")[1]
    assert "name: api-explorer" in front
    assert "description:" in front
