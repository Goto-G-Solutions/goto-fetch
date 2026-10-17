"""Turn records into a dataframe, and work on frames, in any library narwhals supports."""

import json
import logging
import sys
from collections.abc import Iterable, Sequence
from importlib.util import find_spec
from types import ModuleType
from typing import Any, Literal, cast

import narwhals.stable.v2 as nw

from .logs import log_event
from .models import as_dict

logger = logging.getLogger(__name__)

BACKENDS = ("polars", "pandas", "pyarrow")

INT64 = range(-(2**63), 2**63)
"""The integers every dataframe library can hold as integers."""
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
    seen, and a missing value is null. If no record has any key at all,
    the frame is empty: a frame can't hold rows without columns.

    The libraries disagree about messy columns. polars rejects a value whose
    type first shows up after 100 nulls, and pyarrow rejects "123" next to
    456. So types are evened out here first, at every depth: the fields of
    nested objects and the elements of lists are evened out the same way as
    top-level columns. Values of mixed types become text, and a warning
    names where, such as "meta.size" or "tags[*]". Ints and floats count as
    one type, numbers: ints beside floats become floats. Integers too large
    for 64 bits become text, with a warning, so no digit is lost.
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
        if len(found) > 1:
            # Ints and floats share one kind (float), so name it "number".
            names = ", ".join(
                sorted("number" if t is float else t.__name__ for t in found)
            )
            log_event(
                logger,
                logging.WARNING,
                "text",
                "Column %r mixes %s; storing it as text",
                where,
                names,
                data={"column": where, "reason": f"mixes {names}"},
            )
            return [as_text(v) for v in values]
        if found == {float}:
            return even_out_numbers(where, values)
        return values

    def even_out_numbers(where: str, values: list) -> list:
        ints = [v for v in values if isinstance(v, int)]
        if any(v not in INT64 for v in ints):
            # pyarrow can't hold these at all; text keeps every digit.
            log_event(
                logger,
                logging.WARNING,
                "text",
                "Column %r holds integers too large for 64 bits; storing it as text",
                where,
                data={"column": where, "reason": "integers too large for 64 bits"},
            )
            return [as_text(v) for v in values]
        if ints and len(ints) < sum(v is not None for v in values):
            # Ints beside floats become floats, as the libraries do themselves;
            # inside a list some of them refuse the mix.
            return [float(v) if isinstance(v, int) else v for v in values]
        return values

    records = [as_dict(record) for record in records]
    columns = list(dict.fromkeys(key for record in records for key in record))
    data = {c: even_out(c, [r.get(c) for r in records]) for c in columns}
    # narwhals types `backend` with names of its own; any library name or
    # module it accepts is fine here, and it reports one it doesn't know.
    chosen = cast("Any", backend or default_backend())
    if nw.Implementation.from_backend(chosen) is nw.Implementation.POLARS:
        # polars types a list column by its first value, and fails with an
        # internal panic when that value holds only nulls deeper down:
        # [[None]] then [[True]]. strict=False lets it widen the type; the
        # types were already evened out above, so nothing else is loosened.
        import polars as pl

        return pl.DataFrame(data, strict=False)
    return nw.from_dict(data, backend=chosen).to_native()


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
