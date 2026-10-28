"""Test setup for the CLI. Every test gets its own MEERTEX_HOME, MEERTEX_PAPERS
and XDG_CONFIG_HOME, so nothing ever touches the real ~/.meertex, ~/papers or
~/.config/systemd. Every test sees Linux unless it asks for macOS (and a HOME
of its own), so a run on a Mac never touches ~/Library/LaunchAgents either.
The server's tests/conftest.py does not apply here."""

import os
import sys
from pathlib import Path

import pytest

CLI_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CLI_ROOT))

from meertex import service  # after the path above


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("env")
    monkeypatch.setenv("MEERTEX_HOME", str(base / "meertex-home"))
    monkeypatch.setenv("MEERTEX_PAPERS", str(base / "papers"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg"))
    monkeypatch.setattr(service, "MAC", False)
    for name in ("MEERTEX_SERVER", "MEERTEX_TOKEN", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    yield base
    assert not str(os.environ["MEERTEX_HOME"]).startswith(str(Path.home() / ".meertex"))
