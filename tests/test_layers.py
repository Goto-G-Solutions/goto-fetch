"""The layers: each module imports only from modules listed before it.

The list lives in AGENTS.md, section 12 ("Layers"), and is read from
there, so the rule has one home. A module's imports are read from its
source, without importing it.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PACKAGE = ROOT / "src" / "goto_fetch"
APART = {"docs", "explorer", "cli"}
"""The modules that stand apart from the layers (see AGENTS.md)."""


def listed() -> list[str]:
    """The package's modules in the order AGENTS.md lists them, layer by layer."""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text[text.index("**Layers.**") : text.index("**Dependencies.**")]
    names: list[str] = []
    for line in section.splitlines():
        if re.match(r"\d+\. ", line):
            # The modules come before the colon; what follows describes them.
            names += re.findall(r"`(\w+)`", line.partition(":")[0])
    return names


def imported(module: str) -> set[str]:
    """The package's own modules that `module` imports, at any depth in its source."""
    path = PACKAGE / f"{module}.py"
    if not path.exists():
        path = PACKAGE / module / "__init__.py"
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.partition(".")[0])
            else:  # from . import http
                found |= {alias.name for alias in node.names}
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.partition(".")[0] == "goto_fetch"
        ):
            found.add(node.module.partition(".")[2].partition(".")[0])
    return found - {""}


def test_every_module_has_its_place_in_the_layers():
    modules = {
        path.stem if path.is_file() else path.name
        for path in PACKAGE.iterdir()
        if (path.suffix == ".py" or (path / "__init__.py").exists())
        and path.stem != "__init__"
    }
    assert modules - APART == set(listed())
    assert len(listed()) == len(set(listed()))


@pytest.mark.parametrize("module", listed())
def test_a_module_imports_only_from_modules_listed_before_it(module):
    order = listed()
    before = set(order[: order.index(module)])
    assert imported(module) <= before, (
        f"{module} imports {sorted(imported(module) - before)}, which AGENTS.md "
        "lists after it"
    )


def test_the_modules_that_stand_apart_import_only_what_agents_md_allows():
    assert imported("docs") == set()
    assert imported("explorer") == set()
    assert imported("cli") == {"docs", "explorer"}
