"""Optional extras: a missing one fails with an error that names the extra to install.

Each message has one form: "X needs Y: install the extra, goto-fetch[Z]".
The other optional pieces are checked beside their own tests: DuckDB in
test_duckdb_warehouse, sqlite3 in test_state and test_sqlite_warehouse,
the JSON parsers in test_decode, and marimo in test_cli.
"""

import importlib
import sys

import pytest


def test_scrape_without_its_extra_says_how_to_install_it(monkeypatch):
    # Regression: the error was a bare "No module named 'parsel'".
    monkeypatch.setitem(sys.modules, "parsel", None)
    monkeypatch.delitem(sys.modules, "goto_fetch.scrape", raising=False)
    with pytest.raises(ImportError, match=r"install the extra, goto-fetch\[scrape\]"):
        importlib.import_module("goto_fetch.scrape")


def test_httpx2_transport_without_its_extra_says_what_to_install(monkeypatch):
    from goto_fetch import Httpx2Transport

    monkeypatch.setitem(sys.modules, "httpx2", None)
    with pytest.raises(ImportError, match=r"install the extra, goto-fetch\[httpx2\]"):
        Httpx2Transport()


def test_a_frame_without_any_dataframe_library_names_the_ones_to_install(monkeypatch):
    from goto_fetch import frames

    for name in frames.BACKENDS:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(frames, "find_spec", lambda name: None)
    with pytest.raises(ImportError, match="install one of: polars, pandas, pyarrow"):
        frames.default_backend()
