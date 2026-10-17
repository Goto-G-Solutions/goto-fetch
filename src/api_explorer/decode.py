"""Parse JSON bodies, with a faster parser when one is installed."""

import importlib
import json
from collections.abc import Callable
from typing import Any, Literal

Decoder = Callable[[bytes], Any]
"""Turns a response body into Python values, raising ValueError on bad JSON."""


def json_decoder(
    prefer: Literal["auto", "json", "msgspec", "orjson"] = "auto",
) -> Decoder:
    """Return a JSON decoder for response bodies.

    - "json": the standard library.
    - "auto" (the default): msgspec when it is installed, else the standard
      library. msgspec gives the same values as the standard library, only
      faster.
    - "msgspec" or "orjson": that parser, which must be installed. orjson
      turns integers beyond 64 bits into floats without warning, losing
      digits, so choose it only when no value can be that large.

    The standard library reads a few things strict parsers refuse, such as
    NaN, Infinity and numbers too large for a float. A fast decoder falls
    back to it for those, so every choice reads the same bodies.
    """
    if prefer == "json":
        return json.loads
    if prefer == "auto":
        try:
            msgspec = importlib.import_module("msgspec")
        except ImportError:
            return json.loads
        return _with_fallback(msgspec.json.decode)
    if prefer == "msgspec":
        return _with_fallback(_require("msgspec").json.decode)
    if prefer == "orjson":
        return _with_fallback(_require("orjson").loads)
    raise ValueError(
        f"prefer must be 'auto', 'json', 'msgspec' or 'orjson', got {prefer!r}"
    )


def _require(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ImportError(
            f"{name} isn't installed; install it with: pip install {name}"
        ) from exc


def _with_fallback(fast: Decoder) -> Decoder:
    def decode(body: bytes) -> Any:
        try:
            return fast(body)
        except ValueError:
            return json.loads(body)

    return decode
