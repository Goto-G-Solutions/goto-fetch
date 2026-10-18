"""Typed records: converter and as_dict, and records(model=) and to_frame using them."""

import dataclasses
from datetime import datetime

import narwhals.stable.v2 as nw
import pytest

from api_explorer import ApiError, to_frame
from api_explorer.models import as_dict, converter
from api_explorer.testing import json_pages

BASE = "http://api.test/api/items/"


@dataclasses.dataclass
class ItemDataclass:
    id: int
    name: str


def model_kinds():
    """One Item model per supported kind, for those installed."""
    kinds = [pytest.param(ItemDataclass, id="dataclass")]
    try:
        import pydantic

        class ItemPydantic(pydantic.BaseModel):
            id: int
            name: str

        kinds.append(pytest.param(ItemPydantic, id="pydantic"))
    except ImportError:
        pass
    try:
        import msgspec

        class ItemStruct(msgspec.Struct):
            id: int
            name: str

        kinds.append(pytest.param(ItemStruct, id="msgspec"))
    except ImportError:
        pass
    return kinds


@pytest.mark.parametrize("model", model_kinds())
def test_converter_builds_each_kind_of_model_and_ignores_extra_keys(model):
    item = converter(model)({"id": 1, "name": "Ann", "added_later": True})
    assert (item.id, item.name) == (1, "Ann")


@pytest.mark.parametrize("model", model_kinds())
def test_as_dict_turns_each_kind_of_model_back_into_a_dict(model):
    item = converter(model)({"id": 1, "name": "Ann"})
    assert as_dict(item) == {"id": 1, "name": "Ann"}


def test_converter_uses_a_function_as_it_is():
    assert converter(str.upper)("ann") == "ANN"


def test_converter_refuses_something_that_isnt_a_model():
    with pytest.raises(TypeError, match="model must be"):
        converter(42)


def test_as_dict_keeps_dates_and_nested_models():
    @dataclasses.dataclass
    class Wrapper:
        when: datetime
        item: ItemDataclass

    record = Wrapper(datetime(2024, 1, 2), ItemDataclass(1, "Ann"))
    assert as_dict(record) == {
        "when": datetime(2024, 1, 2),
        "item": {"id": 1, "name": "Ann"},
    }


def test_records_turn_each_record_into_the_model(mock_client):
    handler = json_pages({BASE: {"results": [{"id": 1, "name": "Ann"}], "next": None}})
    assert list(mock_client(handler).records("/items/", model=ItemDataclass)) == [
        ItemDataclass(1, "Ann")
    ]


def test_a_record_that_doesnt_fit_names_its_position_and_the_model(mock_client):
    handler = json_pages(
        {
            BASE: {"results": [{"id": 1, "name": "Ann"}], "next": "?page=2"},
            f"{BASE}?page=2": {"results": [{"id": 2}], "next": None},
        }
    )
    with pytest.raises(ApiError, match="Record 1 doesn't fit ItemDataclass") as raised:
        list(mock_client(handler).records("/items/", model=ItemDataclass))
    # The model's own error stays attached, for the details.
    assert isinstance(raised.value.__cause__, TypeError)


def test_to_frame_takes_models_as_well_as_dicts(backend):
    frame = to_frame([ItemDataclass(1, "Ann"), {"id": 2, "name": "Bo"}], backend)
    columns = nw.from_native(frame, eager_only=True).to_dict(as_series=False)
    assert columns == {"id": [1, 2], "name": ["Ann", "Bo"]}
