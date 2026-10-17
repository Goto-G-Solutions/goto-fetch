"""Turn records into a dataframe, and work on frames, in any library narwhals supports."""

import json
import logging
import sys
from collections.abc import Iterable, Sequence
from importlib.util import find_spec
from types import ModuleType
from typing import Any, Literal

import narwhals.stable.v2 as nw

from .models import as_dict

logger = logging.getLogger(__name__)

BACKENDS = ("polars", "pandas", "pyarrow")
"""The dataframe libraries default_backend chooses from, in order."""


def default_backend() -> str:
    """Pick the dataframe library to use when none is named.

    The first of polars, pandas and pyarrow already imported in this
    session, so a notebook working in pandas gets pandas. Otherwise the
    first one installed. Raises ImportError when none is.
    """
    for name in BACKENDS:
        if name in sys.modules:
            return name
    for name in BACKENDS:
        if find_spec(name) is not None:
            return name
    raise ImportError(
        "to_frame needs a dataframe library; install one of: " + ", ".join(BACKENDS)
    )


def to_frame(records: Sequence[Any], backend: str | ModuleType | None = None) -> Any:
    """Build a dataframe from records, in the library you name.

    `backend` is a name such as "polars", "pandas" or "pyarrow", or the
    module itself; None picks one (see default_backend). The result is that
    library's own frame. Records can be dicts, or typed models (see
    as_dict). Columns are every key found in any record, in the order first
    seen, and a missing value is null.

    The libraries disagree about messy columns. polars rejects a value whose
    type first shows up after 100 nulls, and pyarrow rejects "123" next to
    456. So types are evened out here first, at every depth: the fields of
    nested objects and the elements of lists are evened out the same way as
    top-level columns. Values of mixed types become text, and a warning
    names where, such as "meta.size" or "tags[*]". Ints and floats count as
    one type, numbers.
    """

    def kinds(values: Iterable[Any]) -> set[type]:
        return {
            bool
            if isinstance(v, bool)
            else float
            if isinstance(v, (int, float))
            else type(v)
            for v in values
            if v is not None
        }

    def as_text(value: Any) -> str | None:
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str)
        return str(value)

    def even_out(where: str, values: list) -> list:
        found = kinds(values)
        if found == {list}:
            # A list's elements, across all records, are one column of their own.
            elements = [x for v in values if v is not None for x in v]
            fixed = iter(even_out(f"{where}[*]", elements))
            return [None if v is None else [next(fixed) for _ in v] for v in values]
        if found == {dict}:
            # So is each field of a nested object.
            keys = dict.fromkeys(k for v in values if v is not None for k in v)
            fields = {
                k: iter(
                    even_out(
                        f"{where}.{k}",
                        [v[k] for v in values if v is not None and k in v],
                    )
                )
                for k in keys
            }
            return [
                None if v is None else {k: next(fields[k]) for k in v} for v in values
            ]
        if len(found) <= 1:
            return values
        # Ints and floats share one kind (float), so name it "number".
        names = ", ".join(sorted("number" if t is float else t.__name__ for t in found))
        logger.warning("Column %r mixes %s; storing it as text", where, names)
        return [as_text(v) for v in values]

    records = [as_dict(record) for record in records]
    columns = list(dict.fromkeys(key for record in records for key in record))
    data = {c: even_out(c, [r.get(c) for r in records]) for c in columns}
    return nw.from_dict(data, backend=backend or default_backend()).to_native()


def latest_per_key(
    frame: Any,
    key: str,
    by: str,
    keep: Literal["latest", "earliest"] = "latest",
) -> Any:
    """Keep one row per value of `key`: the one with the latest (or earliest) `by`.

    Works on any frame narwhals supports and returns the same kind. Rows
    with a null `by` lose to any row that has one. `by` is sorted as it is.
    Parse text dates first (parse_datetime), because text only sorts right
    when every value shares one format and time zone.
    """
    if keep not in ("latest", "earliest"):
        raise ValueError(f"keep must be 'latest' or 'earliest', got {keep!r}")
    df = nw.from_native(frame, eager_only=True)
    return (
        df.sort(by, descending=keep == "latest", nulls_last=True)
        .unique(subset=[key], keep="first", maintain_order=True)
        .to_native()
    )
