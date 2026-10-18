"""The package's guides, shipped with it, for people and coding agents.

Each page covers one piece: what it is for, what the package already
handles, examples that run, and the traps. build-your-own covers making
a piece of your own on each interface. From a shell:

    python -m api_explorer.docs              # the index
    python -m api_explorer.docs paging       # one page
    python -m api_explorer.docs --path       # where the pages are
    python -m api_explorer.docs skill        # an Agent Skill for this package

The pages match the installed version, and need no network.
"""

from importlib.resources import files
from pathlib import Path

INDEX = "README"
"""The page that lists the others."""


def path() -> Path:
    """The folder that holds the pages, as Markdown files."""
    return Path(str(files(__name__)))


def pages() -> list[str]:
    """The names of the guide pages, without ".md", the index first (SKILL.md aside)."""
    names = sorted(
        p.stem for p in path().glob("*.md") if p.stem not in (INDEX, "SKILL")
    )
    return [INDEX, *names]


def read(page: str = INDEX) -> str:
    """The text of one page, by name ("paging"), or the index.

    "skill" gives SKILL.md, the Agent Skill. Raises ValueError, listing the
    pages, for a name that isn't one.
    """
    name = "SKILL" if page.lower() == "skill" else page.removesuffix(".md")
    found = path() / f"{name}.md"
    if not found.is_file():
        raise ValueError(f"No page {page!r}; the pages are: {', '.join(pages())}")
    return found.read_text(encoding="utf-8")
