"""Shared test fixtures: a client wired to fake servers, and dataframe backends."""

import importlib.util
import logging
from collections.abc import Callable
from typing import Any

import pytest

from api_explorer import Client
from api_explorer.testing import FakeApi

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


class Collect(logging.Handler):
    """A handler that keeps every record it is given, for checking log events."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        """Each record's (event, data), for the records logged with log_event."""
        return [(r.event, r.data) for r in self.records if hasattr(r, "event")]


@pytest.fixture
def mock_client() -> Callable[..., Client]:
    """Make a Client for http://api.test/api whose requests go to a handler.

    Backoff is zero, so retries don't slow the tests down.
    """

    def make(handler: Callable, **options: Any) -> Client:
        api = handler if isinstance(handler, FakeApi) else FakeApi(handler)
        return api.client(**options)

    return make


@pytest.fixture
def log_info(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """caplog set to keep this package's INFO lines, such as page progress."""
    caplog.set_level(logging.INFO, logger="api_explorer")
    return caplog
