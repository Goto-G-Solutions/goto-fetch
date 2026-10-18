"""Read values out of nested JSON by path strings such as "users[0].name"."""

import re
from functools import lru_cache
from typing import Any

# Marks "no such key" as distinct from a key whose value is None, so an
# explicit None never sends get_path on to the next fallback.
MISSING = object()

# One piece of a path. The order matters: a quoted key is tried before a
# plain bracket, so ["a.b"] is one key, quotes and all.
_TOKEN = re.compile(
    r"""
      \[\s*(?P<quote>["'])(?P<quoted>(?:\\.|(?!(?P=quote)).)*)(?P=quote)\s*\]
    | \[(?P<bracket>[^\]"']*)\]
    | (?P<name>[^.\[\]|"']+)
    | (?P<alternative>\|)
    | \.
    """,
    re.VERBOSE | re.DOTALL,  # DOTALL: a quoted key may hold line breaks too
)


# Cached because select() reads the same few paths once per record: caching
# halves its time on 100,000 records (see benchmarks/select_paths.py).
@lru_cache(maxsize=1024)
def _alternatives(path: str) -> tuple[tuple[str, ...], ...]:
    """Split a path into its |-separated alternatives, each a tuple of segments."""
    alternatives: list[list[str]] = [[]]
    position = 0
    for match in _TOKEN.finditer(path):
        if match.start() != position:
            break
        position = match.end()
        if match["alternative"]:
            alternatives.append([])
        elif match["quote"]:
            alternatives[-1].append(re.sub(r"\\(.)", r"\1", match["quoted"]))
        elif segment := (match["bracket"] or match["name"] or "").strip():
            alternatives[-1].append(segment)
    if position != len(path):
        raise ValueError(f"Can't read path {path!r}: unexpected {path[position:]!r}")
    return tuple(tuple(segments) for segments in alternatives)


_INDEX = re.compile(r"-?[0-9]+")


def parse_path(path: str) -> list[str]:
    """Split one path into its segments.

    "data.users[0].name" gives ["data", "users", "0", "name"], and
    "items[*].url" gives ["items", "*", "url"]. A key holding dots or other
    path characters goes in quotes: 'by_url["https://x.org/a"]' gives
    ["by_url", "https://x.org/a"]. A key without brackets in it may also go
    in plain brackets, as in "[a.b]".

    Segments stay strings. Whether "0" means a list index or a dict key is
    decided while walking the data, because JSON object keys are always
    strings: {"2024": ...} must be reachable as "2024". "*" always means the
    wildcard, so a key named "*" can't be reached.

    Raises ValueError for a path it can't read, such as an unclosed quote,
    or for "|", which only get_path understands.
    """
    first, *others = _alternatives(path)
    if others:
        raise ValueError(f"parse_path reads one path; {path!r} has alternatives")
    return list(first)


def get_path(data: Any, path: str, default: Any = None) -> Any:
    """Return the value at `path` in `data`, or `default` if it isn't there.

    Only dicts, lists and tuples are walked. A digit segment indexes a list
    or tuple, negative indices included, and is a plain key in a dict. Keys
    holding dots or other path characters go in quotes (see parse_path).

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

    def walk(node: Any, segments: tuple[str, ...]) -> Any:
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
                # Only plain digits, maybe negative, are an index: int()
                # alone would also take "1_0", "+1" and other scripts' digits.
                if not _INDEX.fullmatch(segment):
                    return MISSING
                try:
                    node = node[int(segment)]
                except IndexError:
                    return MISSING
            else:
                return MISSING
        return node

    alternatives = _alternatives(path)
    for segments in alternatives:
        value = walk(data, segments)
        if value is MISSING or (value == [] and "*" in segments):
            continue
        return value
    if default is None and any("*" in segments for segments in alternatives):
        return []
    return default
