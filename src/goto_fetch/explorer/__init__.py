"""The explorer notebook, shipped with the package, and how to open it.

The notebook (notebook.py beside this file) is a marimo app for exploring
an API and copying the fetch as a script. It is never edited where it is
installed: copy_notebook puts a copy next to the user, and open_notebook
runs marimo on it. `gtfetch explore` does both. This module imports
nothing from the package.
"""

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

NOTEBOOK = files(__name__) / "notebook.py"
"""The shipped notebook, as a resource of this package."""


def copy_notebook(to: Path) -> bool:
    """Write the shipped notebook to `to` if nothing is there; return whether it wrote.

    An existing file is left as it is: the user's edits win over a fresh
    copy.
    """
    if to.exists():
        return False
    to.write_text(NOTEBOOK.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def open_notebook(path: Path, run: Callable[[list[str]], int] | None = None) -> int:
    """Open `path` in marimo's editor and return marimo's exit code.

    Raises ImportError, naming the extra to install, when marimo isn't
    installed. `run` executes the command, subprocess.call unless given;
    tests pass a fake.
    """
    if importlib.util.find_spec("marimo") is None:
        raise ImportError(
            "The explorer needs marimo: install the extra, goto-fetch[explorer]"
        )
    command = [sys.executable, "-m", "marimo", "edit", str(path)]
    return (subprocess.call if run is None else run)(command)
