"""The gtfetch command: open the explorer, or print a guide.

    gtfetch explore            # copy the notebook here, if absent, and open it
    gtfetch explore my.py      # the same, at a path of your choice
    gtfetch docs               # the guides' index
    gtfetch docs paging        # one guide

`python -m goto_fetch.docs` prints the guides too, for a shell without
this command on its PATH, such as a coding agent's; both run the same
code. This module imports only goto_fetch.docs and goto_fetch.explorer.
"""

import argparse
import sys
from pathlib import Path

from .docs.__main__ import main as docs_main
from .explorer import copy_notebook, open_notebook


def main(argv: list[str] | None = None) -> int:
    """Run the command named in `argv` (sys.argv without the program by default)."""
    parser = argparse.ArgumentParser(
        prog="gtfetch", description="Open the explorer, or print a guide."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    explore = commands.add_parser(
        "explore", help="copy the explorer notebook here, if absent, and open it"
    )
    explore.add_argument(
        "path",
        nargs="?",
        default="explorer.py",
        type=Path,
        help="where the notebook lives (default: explorer.py here)",
    )
    docs = commands.add_parser("docs", help="print a guide, or the index")
    docs.add_argument("page", nargs="?", help="the page, such as paging")
    docs.add_argument(
        "--path", action="store_true", help="print the folder that holds the pages"
    )
    args = parser.parse_args(argv)
    if args.command == "docs":
        return docs_main(["--path"] if args.path else [args.page] if args.page else [])
    if copy_notebook(args.path):
        print(f"Wrote {args.path}; edit it as you like")
    try:
        return open_notebook(args.path)
    except ImportError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
