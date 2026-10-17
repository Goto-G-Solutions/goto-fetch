"""json_decoder: parsing response bodies, with a faster parser when installed."""

import json
import sys

import pytest

from api_explorer import json_decoder

# Bodies where strict parsers and the standard library differ. Each decoder
# must give what the standard library gives.
EDGE_CASES = {
    "plain": b'{"a": [1, 2.5, "x", null, true], "b": {"c": "\\u00e9"}}',
    "big-int": b'{"n": 123456789012345678901234567890}',
    "nan": b'{"x": NaN}',
    "huge-float": b'{"g": 1e400}',
}


@pytest.mark.parametrize("prefer", ["auto", "json", "msgspec"])
@pytest.mark.parametrize("body", EDGE_CASES.values(), ids=EDGE_CASES.keys())
def test_decoders_read_every_body_as_the_standard_library_does(prefer, body):
    if prefer == "msgspec":
        pytest.importorskip("msgspec")
    assert repr(json_decoder(prefer)(body)) == repr(json.loads(body))


def test_orjson_falls_back_for_what_it_refuses():
    # orjson isn't in the test above: it turns integers beyond 64 bits into
    # floats without an error, so it can't promise the same values. What it
    # refuses, such as NaN, still reads through the fallback.
    pytest.importorskip("orjson")
    assert repr(json_decoder("orjson")(EDGE_CASES["nan"])) == "{'x': nan}"


def test_the_default_is_the_standard_library_without_msgspec(monkeypatch):
    monkeypatch.setitem(sys.modules, "msgspec", None)
    assert json_decoder() is json.loads


def test_asking_for_a_parser_that_isnt_installed_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "orjson", None)
    with pytest.raises(ImportError, match="pip install orjson"):
        json_decoder("orjson")


def test_bad_json_still_raises_value_error():
    for prefer in ("auto", "json"):
        with pytest.raises(ValueError):
            json_decoder(prefer)(b"<html>")


def test_an_unknown_preference_is_refused():
    with pytest.raises(ValueError, match="prefer must be"):
        json_decoder("yaml")
