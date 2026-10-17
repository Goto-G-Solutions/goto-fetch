"""Typed records: turn each record into a model, and models back into dicts.

Works with pydantic models, msgspec Structs and dataclasses, without
importing pydantic or msgspec itself: a model of theirs can only exist once
they are imported.
"""

import dataclasses
import datetime
import decimal
import sys
import uuid
from collections.abc import Callable
from typing import Any


def converter(model: Any) -> Callable[[Any], Any]:
    """Return a function that turns one record (a dict) into `model`.

    - A pydantic model: model.model_validate(record).
    - A msgspec Struct: msgspec.convert(record, model).
    - A dataclass: model(**record), leaving out keys it has no field for,
      since APIs add fields over time.
    - Any other callable: used as it is.

    Conversion raises ValueError or TypeError when a record doesn't fit.
    """
    msgspec = sys.modules.get("msgspec")
    if isinstance(model, type):
        if hasattr(model, "model_validate"):
            return model.model_validate
        if msgspec is not None and issubclass(model, msgspec.Struct):
            return lambda record: msgspec.convert(record, model)
        if dataclasses.is_dataclass(model):
            names = {f.name for f in dataclasses.fields(model) if f.init}
            return lambda record: model(
                **{k: v for k, v in record.items() if k in names}
            )
    if callable(model):
        return model
    raise TypeError(f"model must be a model class or a function, got {model!r}")


def as_dict(record: Any) -> Any:
    """Return a record as a plain dict, whatever kind of record it is.

    A dict comes back as it is. pydantic models, msgspec Structs and
    dataclasses become dicts, nested ones included, keeping dates, times,
    decimals and UUIDs as they are. Anything else comes back unchanged.
    """
    if isinstance(record, dict):
        return record
    if hasattr(record, "model_dump"):
        return record.model_dump()
    msgspec = sys.modules.get("msgspec")
    if msgspec is not None and isinstance(record, msgspec.Struct):
        kept = (
            datetime.datetime,
            datetime.date,
            datetime.time,
            decimal.Decimal,
            uuid.UUID,
        )
        return msgspec.to_builtins(record, builtin_types=kept)
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        return dataclasses.asdict(record)
    return record
