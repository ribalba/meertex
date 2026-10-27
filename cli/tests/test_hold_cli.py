"""``meertex hold``, ``release``, ``sync --hold`` and the held lines of
``status`` (docs/DESIGN.md §10, Holding), against the fake API and the fake
live server of test_cli and test_sync."""

import json
from pathlib import Path

from meertex import config
from meertex import ydoc as y
from test_cli import PROJECT, fake, live, run  # noqa: F401 - fake and live are fixtures
from test_sync import PROJECT as SYNC_PROJECT


def checkout(fake, live, capsys, tmp_path) -> Path:  # noqa: F811 - the fixtures, passed in
    fake.routes[("GET", f"/api/projects/{PROJECT['name']}")] = {**PROJECT, "id": SYNC_PROJECT}
    y.put_text(live.doc, "main.tex", "one\n")
    root = tmp_path / "paper"
    code, _, err = run(capsys, "checkout", PROJECT["name"], str(root))
    assert code == 0, err
    return root


def test_hold_and_release(fake, live, capsys, tmp_path, monkeypatch):  # noqa: F811
    root = checkout(fake, live, capsys, tmp_path)
    (root / "sections").mkdir()
    monkeypatch.chdir(root / "sections")
    code, out, _ = run(capsys, "hold")
    assert code == 0 and "Holding" in out and config.held_since(root)
    code, out, _ = run(capsys, "hold")
    assert "held already" in out

    # Held: our edit stays here, a coauthor's file arrives.
    (root / "main.tex").write_text("one\ntwo\n")
    y.put_text(live.doc, "other.tex", "from a coauthor\n")
    code, out, _ = run(capsys, "sync", "--once", "--json", str(root))
    result = json.loads(out)["checkouts"][0]
    assert code == 0 and result["held"] is True and result["held_files"] == 1
    assert live.text("main.tex") == "one\n"
    assert (root / "other.tex").read_text() == "from a coauthor\n"
    code, out, _ = run(capsys, "sync", "--once", str(root))
    assert "pulled, held, so the changes to 1 file stay here until meertex release" in out

    code, out, _ = run(capsys, "status", "--json")
    state = json.loads(out)[0]
    assert state["held"] is True and state["held_files"] == 1 and state["held_since"]
    code, out, _ = run(capsys, "status")
    assert "not syncing, held" in out and "1 file with held changes" in out

    code, out, _ = run(capsys, "release", "--json")
    released = json.loads(out)[0]
    assert code == 0 and released["was_held"] and released["held_files"] == 1 and released["ok"]
    assert live.text("main.tex") == "one\ntwo\n"
    assert config.held_since(root) is None
    code, out, _ = run(capsys, "release")
    assert code == 0 and "was not held" in out
    assert json.loads(run(capsys, "status", "--json")[1])[0]["held"] is False


def test_sync_hold_holds_first(fake, live, capsys, tmp_path):  # noqa: F811
    root = checkout(fake, live, capsys, tmp_path)
    (root / "main.tex").write_text("one, held\n")
    code, out, err = run(capsys, "sync", "--hold", "--once", str(root))
    assert code == 0 and "Holding" in err and "pulled, held" in out
    assert config.held_since(root) and live.text("main.tex") == "one\n"
    code, out, _ = run(capsys, "release", str(root))
    assert "Released" in out and "1 held file went to the server" in out
    assert live.text("main.tex") == "one, held\n"


def test_hold_needs_a_checkout(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, err = run(capsys, "hold")
    assert code == 2 and "not inside a meertex checkout" in err
