"""Turn records into a dataframe, and work on frames, in any library narwhals supports."""

import json
import logging
from collections.abc import Iterable, Sequence
from types import ModuleType
from typing import Any, Literal

import narwhals.stable.v2 as nw

logger = logging.getLogger(__name__)


def to_frame(records: Sequence[dict], backend: str | ModuleType) -> Any:
    """Build a dataframe from records, in the library you name.

    `backend` is a name such as "polars", "pandas" or "pyarrow", or the
    module itself. The result is that library's own frame. Columns are every
    key found in any record, in the order first seen, and a missing value is
    null.

    The libraries disagree about messy columns. polars rejects a value whose
    type first shows up after 100 nulls, and pyarrow rejects "123" next to
    456. So types are evened out here first. A column whose values mix types
    becomes text, and so do list elements of mixed types; a warning names the
    column. Ints and floats count as one type, numbers.
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

    def even_out(column: str, values: list) -> list:
        found = kinds(values)
        if found == {list}:
            if len(kinds(x for v in values if v for x in v)) <= 1:
                return values
            logger.warning(
                "Column %r has lists of mixed types; storing as text",
                column,
            )
            return [None if v is None else [as_text(x) for x in v] for v in values]
        if len(found) <= 1:
            return values
        # Ints and floats share one kind (float), so name it "number".
        names = ", ".join(sorted("number" if t is float else t.__name__ for t in found))
        logger.warning("Column %r mixes %s; storing it as text", column, names)
        return [as_text(v) for v in values]

    columns = list(dict.fromkeys(key for record in records for key in record))
    data = {c: even_out(c, [r.get(c) for r in records]) for c in columns}
    return nw.from_dict(data, backend=backend).to_native()


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
