"""meertex update and the daily look for a newer version (cli/meertex/cmd_update.py),
with PyPI, the installer and the service faked."""

import io
import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from meertex import cmd_update, config, service
from test_cli import run

PIPX = {"how": "pipx", "from": "/home/u/.local/share/pipx/venvs/meertex/lib/python3.13/site-packages",
        "command": ["pipx", "upgrade", "meertex"], "hint": None}


@pytest.fixture
def pypi(monkeypatch):
    """PyPI has 0.1.0.3, this is 0.1.0.1 from pipx, and the update brings 0.1.0.3."""
    calls = {"fetch": 0, "run": [], "restart": 0}

    def fetch(transport=None):
        calls["fetch"] += 1
        return "0.1.0.3"

    def fake_run(argv, **kwargs):
        calls["run"].append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def restart():
        calls["restart"] += 1
        return True

    monkeypatch.setattr(cmd_update, "__version__", "0.1.0.1")
    monkeypatch.setattr(cmd_update, "fetch_latest", fetch)
    monkeypatch.setattr(cmd_update, "install", lambda: dict(PIPX))
    monkeypatch.setattr(cmd_update, "installed_version", lambda: "0.1.0.3")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(service, "restart", restart)
    return calls


@pytest.fixture
def terminal(monkeypatch):
    """A person at a terminal with the daily look on. Call it with the answer
    to the question. No answer is the end of stdin."""
    monkeypatch.delenv("MEERTEX_NO_UPDATE_CHECK")
    monkeypatch.setattr(cmd_update, "_terminal", lambda: True)

    def answer(text: str) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(text))

    answer("")
    return answer


def test_versions_compare():
    assert cmd_update.newer("0.1.0.3", "0.1.0.1")
    assert cmd_update.newer("0.1.0.10", "0.1.0.9")
    assert cmd_update.newer("0.1.0.1", "0.1.0")
    assert not cmd_update.newer("0.1.0.1", "0.1.0.1")
    assert not cmd_update.newer("0.1.0", "0.1.0.3")
    assert not cmd_update.newer("0.2.0rc1", "0.1.0")
    assert not cmd_update.newer(None, "0.1.0")


def test_fetch_latest_reads_pypi():
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url == cmd_update.PYPI_URL
        return httpx.Response(200, json={"info": {"version": "0.1.0.3"}, "releases": {}})

    assert cmd_update.fetch_latest(httpx.MockTransport(answer)) == "0.1.0.3"
    with pytest.raises(ValueError):
        cmd_update.fetch_latest(httpx.MockTransport(lambda r: httpx.Response(200, json={"info": {}})))
    with pytest.raises(httpx.HTTPError):
        cmd_update.fetch_latest(httpx.MockTransport(lambda r: httpx.Response(503)))


def test_the_tests_run_meertex_from_source():
    assert cmd_update.install()["how"] == "source"


def test_update_runs_the_installer_and_restarts_the_service(pypi, capsys):
    code, out, err = run(capsys, "update")
    assert code == 0
    assert pypi["run"] == [["pipx", "upgrade", "meertex"]]
    assert pypi["restart"] == 1
    assert "Updated to meertex 0.1.0.3." in out
    assert "Restarted meertex.service, so the background sync runs it too." in out
    assert "Running pipx upgrade meertex" in err
    assert json.loads((config.home() / "update.json").read_text())["latest"] == "0.1.0.3"


def test_update_check_only_tells(pypi, capsys):
    code, out, _ = run(capsys, "update", "--check")
    assert code == 0 and pypi["run"] == []
    assert "meertex 0.1.0.3 is out, you have 0.1.0.1. Update with meertex update." in out
    code, out, _ = run(capsys, "update", "--check", "--json")
    data = json.loads(out)
    assert data["newer"] is True and data["latest"] == "0.1.0.3" and data["installed"] == "0.1.0.1"
    assert data["how"] == "pipx" and data["command"] == ["pipx", "upgrade", "meertex"]


def test_update_when_it_is_the_newest(pypi, capsys, monkeypatch):
    monkeypatch.setattr(cmd_update, "__version__", "0.1.0.3")
    code, out, _ = run(capsys, "update")
    assert code == 0 and pypi["run"] == [] and pypi["restart"] == 0
    assert "meertex 0.1.0.3 is the newest version." in out


def test_a_failed_update_leaves_the_service_alone(pypi, capsys, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1))
    code, out, _ = run(capsys, "update", "--json")
    data = json.loads(out)
    assert code == 1 and data["updated"] is False and pypi["restart"] == 0
    assert data["error"] == "The update failed with exit status 1."


def test_update_leaves_a_source_install_alone(pypi, capsys, monkeypatch):
    monkeypatch.setattr(cmd_update, "install", lambda: {
        "how": "source", "from": "/src/meertex/cli", "command": None,
        "hint": "It runs from a source folder. Update it there."})
    code, out, _ = run(capsys, "update")
    assert code == 0 and pypi["fetch"] == 0 and pypi["run"] == []
    assert out == "This is meertex 0.1.0.1. It runs from a source folder. Update it there.\n"


def test_the_daily_look_offers_the_update(pypi, terminal, capsys):
    terminal("y\n")
    code, out, err = run(capsys, "status")
    assert code == 0
    assert "No checkouts on this machine" in out
    assert "meertex 0.1.0.3 is out, you have 0.1.0.1. Update now? [Y/n]" in err
    assert pypi["run"] == [["pipx", "upgrade", "meertex"]] and pypi["restart"] == 1
    assert "Updated to meertex 0.1.0.3." in out
    # Once a day.
    terminal("y\n")
    run(capsys, "status")
    assert pypi["fetch"] == 1 and len(pypi["run"]) == 1


def test_saying_no_asks_again_tomorrow(pypi, terminal, capsys):
    terminal("n\n")
    _, _, err = run(capsys, "status")
    assert pypi["run"] == []
    assert "Update later with meertex update. meertex asks again tomorrow." in err
    run(capsys, "status")
    assert pypi["fetch"] == 1
    cmd_update.write_state(checked_at=(datetime.now(UTC) - timedelta(days=1, minutes=1)).isoformat())
    run(capsys, "status")
    assert pypi["fetch"] == 2


def test_no_daily_look_for_scripts(pypi, terminal, capsys, monkeypatch):
    run(capsys, "status", "--json")
    assert pypi["fetch"] == 0
    monkeypatch.setattr(cmd_update, "_terminal", lambda: False)
    run(capsys, "status")
    assert pypi["fetch"] == 0
    monkeypatch.setattr(cmd_update, "_terminal", lambda: True)
    config.write_config({"update_check": False})
    run(capsys, "status")
    assert pypi["fetch"] == 0


def test_pypi_down_costs_one_try_a_day(pypi, terminal, capsys, monkeypatch):
    def down(transport=None):
        pypi["fetch"] += 1
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(cmd_update, "fetch_latest", down)
    code, _, err = run(capsys, "status")
    assert code == 0 and err == ""
    run(capsys, "status")
    assert pypi["fetch"] == 1


def test_an_install_meertex_cannot_update_gets_a_hint(pypi, terminal, capsys, monkeypatch):
    monkeypatch.setattr(cmd_update, "install", lambda: {
        "how": "system", "from": "/usr/lib/python3/site-packages", "command": None,
        "hint": "It is installed for all users in /usr/lib/python3/site-packages. Update it the way it was installed."})
    _, _, err = run(capsys, "status")
    assert "meertex 0.1.0.3 is out, you have 0.1.0.1. It is installed for all users" in err
    assert "[Y/n]" not in err and pypi["run"] == []


def test_restart_only_a_running_service(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(service, "status", lambda lines=20: {"running": False})
    assert service.restart() is False and calls == []
    monkeypatch.setattr(service, "status", lambda lines=20: {"running": True})
    assert service.restart() is True
    assert calls == [["systemctl", "--user", "restart", "meertex.service"]]
    monkeypatch.setattr(service, "MAC", True)
    calls.clear()
    assert service.restart() is True
    assert calls[0][:3] == ["launchctl", "kickstart", "-k"]
