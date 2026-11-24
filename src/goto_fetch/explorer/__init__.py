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
    copy. So is anything else by that name, a link that leads nowhere
    included. Raises OSError when the file can't be written, such as into
    a folder that isn't there.
    """
    if to.is_symlink():
        # Asked for by name: Windows follows a link that leads nowhere and
        # writes its target, even with "x".
        return False
    try:
        # "x" makes the file only if the name is free, in one step.
        with to.open("x", encoding="utf-8") as file:
            file.write(NOTEBOOK.read_text(encoding="utf-8"))
    except FileExistsError:
        return False
    return True


def require_marimo() -> None:
    """Raise ImportError, naming the extra to install, when marimo isn't installed."""
    if importlib.util.find_spec("marimo") is None:
        raise ImportError(
            "The explorer needs marimo: install the extra, goto-fetch[explorer]"
        )


def open_notebook(path: Path, run: Callable[[list[str]], int] | None = None) -> int:
    """Open `path` in marimo's editor and return marimo's exit code.

    Raises ImportError, naming the extra to install, when marimo isn't
    installed (see require_marimo). `run` executes the command,
    subprocess.call unless given; tests pass a fake.
    """
    require_marimo()
    # --no-sandbox: this environment already holds the package and marimo;
    # a sandbox would resolve goto-fetch again, from PyPI, and marimo would
    # otherwise prompt for one whenever uv is installed.
    command = [sys.executable, "-m", "marimo", "edit", "--no-sandbox", str(path)]
    return (subprocess.call if run is None else run)(command)
