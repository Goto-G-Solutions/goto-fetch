"""Print a page of the guides: python -m api_explorer.docs [page | --path]."""

import sys

from . import path, read


def main(argv: list[str]) -> int:
    """Print the page named in `argv` (the index if none), or the folder with --path."""
    if argv[:1] == ["--path"]:
        print(path())
        return 0
    try:
        print(read(argv[0] if argv else "README"))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
