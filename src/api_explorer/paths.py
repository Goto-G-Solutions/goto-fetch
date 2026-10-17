"""Read values out of nested JSON by path strings such as "users[0].name"."""

import re
from typing import Any

# Marks "no such key" as distinct from a key whose value is None, so an
# explicit None never sends get_path on to the next fallback.
MISSING = object()


def parse_path(path: str) -> list[str]:
    """Split one path into its segments.

    "data.users[0].name" gives ["data", "users", "0", "name"], and
    "items[*].url" gives ["items", "*", "url"].

    Segments stay strings. Whether "0" means a list index or a dict key is
    decided while walking the data, because JSON object keys are always
    strings: {"2024": ...} must be reachable as "2024".
    """
    return [segment for segment in re.split(r"[.\[\]]+", path) if segment]


def get_path(data: Any, path: str, default: Any = None) -> Any:
    """Return the value at `path` in `data`, or `default` if it isn't there.

    Only dicts, lists and tuples are walked. A digit segment indexes a list
    or tuple, negative indices included, and is a plain key in a dict.

    `[*]` collects the rest of the path from every element of a list. An
    element without the value contributes None, so the result lines up with
    the list.

    Alternatives separated by `|` are tried in order, and the first that
    exists wins. Two rules decide "exists":

    - A key whose value is None exists. None is an answer, not an absence.
    - A wildcard that collects nothing does not exist. An empty list means
      there is no data here, so the next alternative is tried.

    When nothing exists, a wildcard path returns [] unless you pass a
    default, so a column of lists never has a stray None in it.
    """

    def walk(node: Any, segments: list[str]) -> Any:
        for i, segment in enumerate(segments):
            if segment == "*":
                if not isinstance(node, (list, tuple)):
                    return MISSING
                rest = segments[i + 1 :]
                found = (walk(item, rest) for item in node)
                return [None if value is MISSING else value for value in found]
            if isinstance(node, dict):
                if segment not in node:
                    return MISSING
                node = node[segment]
            elif isinstance(node, (list, tuple)):
                try:
                    node = node[int(segment)]
                except (ValueError, IndexError):
                    return MISSING
            else:
                return MISSING
        return node

    alternatives = [alternative.strip() for alternative in path.split("|")]
    for alternative in alternatives:
        value = walk(data, parse_path(alternative))
        if value is MISSING or (value == [] and "[*]" in alternative):
            continue
        return value
    if default is None and any("[*]" in a for a in alternatives):
        return []
    return default
