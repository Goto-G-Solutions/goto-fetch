<!-- needs: polars -->
# Records and frames

Records are parsed JSON: lists of dicts. These functions reshape them without a dataframe library, and `to_frame` turns them into one at the end.

## What it handles for you

- **Paths:** `get_path` reads nested values with fallbacks (`"user.email|contact.email"`), wildcards (`"tags[*].name"`) and quoted keys (`'["@odata.id"]'`); a missing value is `None`, never an error.
- **Messy types:** `to_frame` evens out mixed types at every depth before building the frame, so polars, pandas and pyarrow all take the same records. Mixed values become text with a warning naming the column; integers too large for 64 bits become text, so no digit is lost.

## Examples

```python
from api_explorer import explode, flatten, get_path, select, to_frame

records = [
    {"id": 1, "user": {"name": "Ann"}, "tags": [{"name": "new"}, {"name": "vip"}]},
    {"id": 2, "user": None, "tags": []},
]

assert get_path(records[0], "user.name") == "Ann"
assert get_path(records[0], "tags[*].name") == ["new", "vip"]
assert get_path(records[1], "user.name|id") == 2  # a fallback

rows = select(records, ["id", "user.name", "tags[*].name"])
assert rows[0] == {"id": 1, "user.name": "Ann", "tags[*].name": ["new", "vip"]}

assert flatten({"user": {"name": "Ann"}, "tags": ["a"]}) == {
    "user.name": "Ann",
    "tags[0]": "a",
}

long = explode([{"id": 1, "tags": ["a", "b"]}, {"id": 2, "tags": []}], columns=["tags"])
assert long == [{"id": 1, "tags": "a"}, {"id": 1, "tags": "b"}, {"id": 2, "tags": None}]

# "pandas" and "pyarrow" work too, and None picks whichever is installed
frame = to_frame(rows, "polars")
assert frame.shape == (2, 3)
```

Typed records come from `records(model=...)`: a pydantic model, msgspec Struct, dataclass or function.

## Traps

- `explode`'s columns are top-level keys; `flatten` first to reach nested lists.
- A key holding the path separator or path characters is quoted by `flatten` and in warnings (`meta["a.b"]`), so it reads back with `get_path`.
- A key named exactly `*` can't be reached by a path, since `*` is the wildcard.
- Parse text dates (`parse_datetime`) before sorting by them: text sorts right only when every value shares one format and zone.
