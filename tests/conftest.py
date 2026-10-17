"""Shared test fixtures: a client wired to fake servers, and dataframe backends."""

import importlib.util
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from api_explorer import Client, Retry

# A deeper search for the property tests, for occasional runs:
#   uv run pytest tests/test_properties.py --hypothesis-profile=thorough
# Hypothesis is a dev dependency, so the core-only run goes without it.
if importlib.util.find_spec("hypothesis"):
    from hypothesis import settings

    settings.register_profile("thorough", max_examples=2000, deadline=None)

# Frame tests run once per dataframe library installed. The dev group
# installs polars and pandas, so both are always tried in development and CI.
BACKENDS = [
    name for name in ("polars", "pandas", "pyarrow") if importlib.util.find_spec(name)
]


@pytest.fixture(
    params=BACKENDS
    or [pytest.param(None, marks=pytest.mark.skip(reason="no dataframe library"))]
)
def backend(request: pytest.FixtureRequest) -> str:
    """The name of one installed dataframe library."""
    return request.param


@pytest.fixture
def mock_client() -> Callable[..., Client]:
    """Make a Client for http://api.test/api whose requests go to a handler.

    Backoff is zero, so retries don't slow the tests down.
    """

    def make(handler: Callable, **options: Any) -> Client:
        options.setdefault("retry", Retry(backoff=0.0))
        return Client(
            "http://api.test/api", transport=httpx2.MockTransport(handler), **options
        )

    return make


@pytest.fixture
def log_info(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """caplog set to keep this package's INFO lines, such as page progress."""
    caplog.set_level(logging.INFO, logger="api_explorer")
    return caplog
