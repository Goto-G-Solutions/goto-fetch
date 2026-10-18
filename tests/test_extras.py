"""Optional extras: a missing one fails with the command that installs it."""

import importlib
import sys

import pytest


def test_scrape_without_its_extra_says_how_to_install_it(monkeypatch):
    # Regression: the error was a bare "No module named 'parsel'".
    monkeypatch.setitem(sys.modules, "parsel", None)
    monkeypatch.delitem(sys.modules, "goto_fetch.scrape", raising=False)
    with pytest.raises(ImportError, match=r'pip install "goto-fetch\[scrape\]"'):
        importlib.import_module("goto_fetch.scrape")
