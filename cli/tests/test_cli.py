"""The commands against a fake HTTP API (httpx.MockTransport): the request each
one sends, what it prints, and the local files it writes."""

import asyncio
import io
import json
import stat
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from meertex import cli, config, service
from meertex import ydoc as y
from meertex.api import Api

SERVER = "http://meertex.test"


class FakeApi:
    """Answers from a route table and records every request."""

    def __init__(self):
        self.routes: dict = {}
        self.calls: list[tuple[str, str, dict, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: object = request.content
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content)
        self.calls.append((request.method, request.url.path, dict(request.url.params), body))
        answer = self.routes.get((request.method, request.url.path))
        if callable(answer):
            answer = answer(request)
        if answer is None:
            return httpx.Response(404, json={"detail": "Not found"})
        if isinstance(answer, httpx.Response):
            return answer
        status, data = answer if isinstance(answer, tuple) else (200, answer)
        return httpx.Response(status, json=data)

    def api(self, server: str = SERVER, token: str | None = "tok") -> Api:
        return Api(server, token, transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake(monkeypatch):
    f = FakeApi()
    monkeypatch.setenv("MEERTEX_SERVER", SERVER)
    monkeypatch.setenv("MEERTEX_TOKEN", "tok")
    monkeypatch.setattr(cli, "api", lambda: f.api())
    monkeypatch.setattr(cli, "Api", lambda server, token: f.api(server, token))
    return f


def run(capsys, *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv))
    captured = capsys.readouterr()
    return exc.value.code, captured.out, captured.err


PROJECT = {
    "id": "11111111-2222-3333-4444-555555555555",
    "name": "2027-icse-green-llm",
    "title": "Green LLM inference",
    "conference": "ICSE",
    "year": 2027,
    "outcome": None,
    "main_file": "main.tex",
    "url": f"{SERVER}/p/2027-icse-green-llm",
    "dates": [
        {"id": "d1aaaaaa-0000", "kind": "abstract", "label": "Abstract registration", "due_at": "2027-01-08T11:59:59Z",
         "tz": "AoE", "date_only": True, "local": "2027-01-07T23:59:59-12:00", "seconds_left": -100},
        {"id": "d2bbbbbb-0000", "kind": "submission", "label": "Paper submission", "due_at": "2027-01-15T11:59:59Z",
         "tz": "AoE", "date_only": True, "local": "2027-01-14T23:59:59-12:00", "seconds_left": 2 * 86400},
    ],
    "versions": [],
    "questions": [],
}


# --- Parsing -------------------------------------------------------------------------


def test_when_specs():
    assert cli.parse_when("2027-01-15") == {"date": "2027-01-15", "tz": "AoE"}
    assert cli.parse_when("2027-01-15T9:30@Europe/Berlin") == {"due_at": "2027-01-15T09:30", "tz": "Europe/Berlin"}
    assert cli.parse_when("2027-01-15T23:59@UTC+2") == {"due_at": "2027-01-15T23:59", "tz": "UTC+2"}
    for bad in ("15.01.2027", "2027-02-30", "2027-01-15@", "tomorrow"):
        with pytest.raises(cli.UsageError):
            cli.parse_when(bad)


def test_date_specs():
    assert cli.parse_date_spec("submission=2027-01-15") == {"kind": "submission", "date": "2027-01-15", "tz": "AoE"}
    assert cli.parse_date_spec("camera-ready=2027-05-01T12:00@UTC") == {
        "kind": "camera_ready", "due_at": "2027-05-01T12:00", "tz": "UTC"}
    assert cli.parse_date_spec("other:Artifact=2027-03-01")["label"] == "Artifact"
    for bad in ("deadline=2027-01-15", "other=2027-01-15", "submission"):
        with pytest.raises(cli.UsageError):
            cli.parse_date_spec(bad)


def test_fields():
    assert cli.parse_field("year=2027") == ("year", 2027)
    assert cli.parse_field("labels=a, b") == ("labels", ["a", "b"])
    assert cli.parse_field("archived=yes") == ("archived", True)
    assert cli.parse_field("outcome=none") == ("outcome", None)
    assert cli.parse_field("conference-name=Int. Conf.") == ("conference_name", "Int. Conf.")
    with pytest.raises(cli.UsageError):
        cli.parse_field("titel=x")


# --- Projects --------------------------------------------------------------------------


def test_new_builds_the_body_from_json_and_flags(fake, capsys, tmp_path):
    spec = tmp_path / "cfp.json"
    spec.write_text(json.dumps({
        "title": "Old title", "conference": "ICSE", "year": 2027, "notes": "from the CFP",
        "dates": [{"kind": "abstract", "date": "2027-01-08"}, {"kind": "submission", "date": "2027-01-10"}],
    }))
    fake.routes[("POST", "/api/projects")] = (201, PROJECT)
    code, out, _ = run(capsys, "new", "--from-json", str(spec), "--title", "Green LLM inference",
                       "--date", "submission=2027-01-15T23:59@Europe/Berlin", "--set", "track=Research Track",
                       "--label", "green", "--label", "with-arne", "--folder", "2027/Submitted", "--json")
    assert code == 0
    body = fake.calls[0][3]
    assert body["title"] == "Green LLM inference" and body["notes"] == "from the CFP"
    assert body["track"] == "Research Track" and body["labels"] == ["green", "with-arne"]
    assert body["folder"] == "2027/Submitted"
    assert body["dates"] == [{"kind": "abstract", "date": "2027-01-08"},
                             {"kind": "submission", "due_at": "2027-01-15T23:59", "tz": "Europe/Berlin"}]
    printed = json.loads(out)
    assert printed["name"] == PROJECT["name"] and printed["checkout_dir"] is None


def test_new_dry_run_prints_the_name(fake, capsys):
    fake.routes[("POST", "/api/projects")] = {"name": "2027-icse-green-llm-2", "dry_run": True}
    code, out, _ = run(capsys, "new", "--title", "Green LLM", "--conference", "ICSE", "--year", "2027", "--dry-run")
    assert code == 0 and out.strip() == "2027-icse-green-llm-2"
    assert fake.calls[0][3]["dry_run"] is True


def test_fork_posts_to_the_old_project(fake, capsys):
    fake.routes[("POST", f"/api/projects/{PROJECT['name']}/fork")] = (201, {**PROJECT, "name": "2027-fse-green-llm"})
    code, out, _ = run(capsys, "fork", PROJECT["name"], "--conference", "FSE", "--date", "submission=2027-03-01")
    assert code == 0 and "Forked into 2027-fse-green-llm" in out
    assert fake.calls[0][3] == {"conference": "FSE", "dates": [{"kind": "submission", "date": "2027-03-01", "tz": "AoE"}]}


def test_projects_table_and_filters(fake, capsys):
    fake.routes[("GET", "/api/projects")] = [{
        **PROJECT, "next_date": {"kind": "submission", "label": "Paper submission", "seconds_left": 2 * 86400},
        "question_count": 1, "labels": [],
    }]
    code, out, _ = run(capsys, "projects", "--all", "--q", "llm", "--year", "2027", "--conference", "ICSE")
    assert code == 0
    assert fake.calls[0][2] == {"archived": "all", "q": "llm", "year": "2027", "conference": "ICSE"}
    row = out.splitlines()[1]
    assert row.startswith("2027-icse-green-llm") and "Paper submission  in 2 days" in row and "1 ?" in row
    run(capsys, "projects", "--archived")
    assert fake.calls[1][2] == {"archived": "true"}


def test_show_prints_dates_with_countdowns(fake, capsys):
    fake.routes[("GET", f"/api/projects/{PROJECT['name']}")] = PROJECT
    code, out, _ = run(capsys, "show", PROJECT["name"])
    assert code == 0
    assert "2027-01-14 AoE" in out and "in 2 days" in out and "ago" in out


def test_set_rejects_unknown_fields(fake, capsys):
    code, _, err = run(capsys, "set", "x", "titel=Oops")
    assert code == 2 and "Unknown field" in err and fake.calls == []


def test_set_patches(fake, capsys):
    fake.routes[("PATCH", "/api/projects/x")] = PROJECT
    code, _, _ = run(capsys, "set", "x", "outcome=accepted", "doi=10.1/abc")
    assert code == 0 and fake.calls[0][3] == {"outcome": "accepted", "doi": "10.1/abc"}


def test_errors_as_json(fake, capsys):
    code, out, _ = run(capsys, "show", "nope", "--json")
    assert code == 1 and json.loads(out)["status"] == 404


# --- Dates, questions, versions -----------------------------------------------------------


def test_answer_by_kind(fake, capsys):
    base = f"/api/projects/{PROJECT['name']}"
    fake.routes[("GET", base)] = PROJECT
    fake.routes[("POST", f"{base}/dates/d2bbbbbb-0000/answer")] = {
        **PROJECT, "versions": [{"id": "v1", "label": "Submission", "tag": "submission", "commit": "abcdef123"}]}
    code, out, _ = run(capsys, "answer", PROJECT["name"], "submission", "yes", "--at", "deadline")
    assert code == 0
    assert fake.calls[-1][3] == {"answer": "yes", "version": {"at": "deadline"}}
    assert "Submission (tag submission" in out


def test_answer_moved_takes_a_date(fake, capsys):
    base = f"/api/projects/{PROJECT['name']}"
    fake.routes[("GET", base)] = PROJECT
    fake.routes[("POST", f"{base}/dates/d1aaaaaa-0000/answer")] = PROJECT
    run(capsys, "answer", PROJECT["name"], "d1aa", "moved", "--date", "2027-01-20")
    assert fake.calls[-1][3] == {"answer": "moved", "date": "2027-01-20", "tz": "AoE"}


def test_date_set_and_rm(fake, capsys):
    base = f"/api/projects/{PROJECT['name']}"
    fake.routes[("GET", base)] = PROJECT
    fake.routes[("PATCH", f"{base}/dates/d2bbbbbb-0000")] = PROJECT["dates"][1]
    fake.routes[("DELETE", f"{base}/dates/d1aaaaaa-0000")] = {"ok": True}
    fake.routes[("POST", f"{base}/dates")] = (201, PROJECT["dates"][1])
    assert run(capsys, "date", PROJECT["name"], "set", "submission", "2027-01-20T12:00@UTC")[0] == 0
    assert fake.calls[-1][3] == {"due_at": "2027-01-20T12:00", "tz": "UTC"}
    assert run(capsys, "date", PROJECT["name"], "rm", "abstract")[0] == 0
    assert fake.calls[-1][:2] == ("DELETE", f"{base}/dates/d1aaaaaa-0000")
    assert run(capsys, "date", PROJECT["name"], "add", "notification=2027-03-01")[0] == 0
    assert fake.calls[-1][3] == {"kind": "notification", "date": "2027-03-01", "tz": "AoE"}


def test_version_with_pdf(fake, capsys, tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 handed in")
    base = f"/api/projects/{PROJECT['name']}/versions"
    fake.routes[("POST", base)] = (201, {"id": "v1", "number": 1, "label": "Submission", "tag": "submission",
                                         "commit": "abc", "pdf_name": None})
    fake.routes[("PUT", f"{base}/v1/pdf")] = {"id": "v1", "number": 1, "label": "Submission", "tag": "submission",
                                               "commit": "abc", "pdf_name": "paper.pdf"}
    code, out, _ = run(capsys, "version", PROJECT["name"], "submission", "--pdf", str(pdf), "--note", "as sent",
                       "--json")
    assert code == 0
    assert fake.calls[0][3] == {"kind": "submission", "note": "as sent"}
    method, path, params, body = fake.calls[1]
    assert (method, path, params, body) == ("PUT", f"{base}/v1/pdf", {"name": "paper.pdf"}, b"%PDF-1.7 handed in")
    assert json.loads(out)["pdf_name"] == "paper.pdf"


# --- Folders ------------------------------------------------------------------------------


FOLDERS = [
    {"id": "f1", "name": "2027", "parent_id": None, "path": "2027", "position": 1},
    {"id": "f2", "name": "Submitted", "parent_id": "f1", "path": "2027/Submitted", "position": 1},
    {"id": "f3", "name": "Accepted", "parent_id": "f1", "path": "2027/Accepted", "position": 2},
]


def test_folder_commands(fake, capsys):
    fake.routes[("GET", "/api/folders")] = FOLDERS
    fake.routes[("POST", "/api/folders")] = (201, {"id": "f4", "path": "2027/Rejected"})
    fake.routes[("PATCH", "/api/folders/f2")] = {"id": "f2", "path": "x"}
    fake.routes[("PUT", "/api/projects/p/placement")] = {"ok": True}
    run(capsys, "folder", "add", "2027/Rejected")
    assert fake.calls[-1][3] == {"name": "Rejected", "parent": "2027"}
    run(capsys, "folder", "mv", "2027/submitted", "2027/Accepted")  # into an existing folder
    assert fake.calls[-1][3] == {"parent_id": "f3"}
    run(capsys, "folder", "mv", "2027/Submitted", "2027/Handed in")  # a rename
    assert fake.calls[-1][3] == {"name": "Handed in", "parent_id": "f1"}
    run(capsys, "folder", "mv", "2027/Submitted", "/")
    assert fake.calls[-1][3] == {"parent_id": None}
    run(capsys, "mv", "p", "2027/Accepted")
    assert fake.calls[-1][3] == {"folder_id": "f3"}
    run(capsys, "mv", "p", "/")
    assert fake.calls[-1][3] == {"folder_id": None}
    _, out, _ = run(capsys, "folders")
    assert out.splitlines() == ["2027", "  Submitted", "  Accepted"]


# --- Login and local configuration --------------------------------------------------------


def test_login_with_code(fake, capsys, monkeypatch):
    fake.routes[("POST", "/api/auth/login")] = {"ok": True}
    fake.routes[("POST", "/api/auth/verify-code")] = lambda r: httpx.Response(
        200, json={"ok": True}, headers={"Set-Cookie": "meertex_session=s3ss; Path=/"})
    fake.routes[("POST", "/api/auth/api-token")] = lambda r: (
        (200, {"email": "didi@example.com", "api_token": "the-token"})
        if "meertex_session=s3ss" in r.headers.get("cookie", "") else (401, {"detail": "Not authenticated"}))
    monkeypatch.delenv("MEERTEX_TOKEN")
    monkeypatch.delenv("MEERTEX_SERVER")
    monkeypatch.setattr("sys.stdin", io.StringIO("ab-c234\n"))
    code, _, err = run(capsys, "login", "--server", "meertex.test", "--email", "didi@example.com")
    assert code == 0, err
    assert fake.calls[0][3] == {"email": "didi@example.com", "client": "cli"}
    assert fake.calls[1][3] == {"email": "didi@example.com", "code": "ab-c234"}
    token_file = config.home() / "token"
    assert token_file.read_text().strip() == "the-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert config.server() == "https://meertex.test"
    assert config.token() == "the-token"


def test_login_with_token(fake, capsys, monkeypatch):
    monkeypatch.delenv("MEERTEX_TOKEN")
    fake.routes[("GET", "/api/auth/me")] = {"email": "didi@example.com"}
    code, out, _ = run(capsys, "login", "--token", "given", "--json")
    assert code == 0 and json.loads(out)["email"] == "didi@example.com"
    assert config.token() == "given"


def test_missing_login_is_a_clear_error(capsys, monkeypatch):
    code, _, err = run(capsys, "projects")
    assert code == 2 and "meertex login" in err


def test_service_install_writes_the_unit_only(capsys, monkeypatch, isolated_home):
    def no_systemctl(*args, **kwargs):
        raise AssertionError("systemctl must not run in tests")

    monkeypatch.setattr(service.subprocess, "run", no_systemctl)
    code, out, _ = run(capsys, "service", "install", "--no-start", "--json")
    assert code == 0
    unit = Path(json.loads(out)["unit"])
    assert unit == isolated_home / "xdg" / "systemd" / "user" / "meertex.service"
    text = unit.read_text()
    assert "ExecStart=" in text and text.split("ExecStart=")[1].split("\n")[0].endswith(" sync")
    assert f"MEERTEX_HOME={config.home().resolve()}" in text
    assert "MEERTEX_TOKEN" not in text
    code, out, _ = run(capsys, "service", "uninstall", "--no-start")
    assert code == 0 and not unit.exists()


UNIT_GONE = {"LoadState": "not-found", "FragmentPath": "", "UnitFileState": "", "ActiveState": "inactive",
             "SubState": "dead", "MainPID": "0", "NRestarts": "0", "Result": "success", "ExecMainStatus": "0",
             "ActiveEnterTimestamp": "", "InactiveEnterTimestamp": ""}
JOURNAL = [
    {"__REALTIME_TIMESTAMP": "1793139254000000", "_PID": "4242", "PRIORITY": "6",
     "MESSAGE": "22:14:14 paper  ↑ main.tex"},
    # journald sends a message with colour codes in it as bytes.
    {"__REALTIME_TIMESTAMP": "1793139255000000", "_PID": "4242", "PRIORITY": "3",
     "MESSAGE": list(b"\x1b[31mlost the server\x1b[0m")},
]


def fake_systemd(monkeypatch, props: dict, journal: list[dict] | None = JOURNAL) -> list[list[str]]:
    """Answers systemctl show and journalctl as systemd would. No journal is
    journalctl failing. Returns the commands that ran."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 0, "".join(f"{k}={v}\n" for k, v in props.items()), "")
        if journal is None:
            return subprocess.CompletedProcess(argv, 1, "", "No journal files were found.")
        return subprocess.CompletedProcess(argv, 0, "".join(json.dumps(e) + "\n" for e in journal), "")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    return calls


def test_service_status_shows_the_unit_and_its_log(capsys, monkeypatch, isolated_home):
    unit = isolated_home / "meertex.service"
    unit.write_text("[Service]\nExecStart=/opt/bin/meertex sync\n")
    calls = fake_systemd(monkeypatch, {
        **UNIT_GONE, "LoadState": "loaded", "FragmentPath": str(unit), "UnitFileState": "enabled",
        "ActiveState": "active", "SubState": "running", "MainPID": "4242",
        "ActiveEnterTimestamp": "Tue 2026-10-27 22:14:14 UTC",
    })
    code, out, _ = run(capsys, "service", "status", "--json", "-n", "5")
    s = json.loads(out)
    assert code == 0 and s["running"] and s["enabled"] and s["pid"] == 4242
    assert s["since"] == "2026-10-27T22:14:14+00:00" and s["command"] == "/opt/bin/meertex sync"
    assert [e["message"] for e in s["logs"]] == ["22:14:14 paper  ↑ main.tex", "\x1b[31mlost the server\x1b[0m"]
    assert ["journalctl", "--user", "--unit=meertex.service", "--lines=5", "--output=json", "--no-pager"] in calls

    code, out, _ = run(capsys, "service", "status")
    assert code == 0 and "running since" in out and "/opt/bin/meertex sync" in out
    # The client's own time stamp goes. The journal's day and time stay.
    assert "  paper  ↑ main.tex" in out and "22:14:14 paper" not in out
    assert "lost the server" in out and "\x1b" not in out


def test_service_status_of_a_failed_unit(capsys, monkeypatch, isolated_home):
    unit = isolated_home / "meertex.service"
    unit.write_text("[Service]\nExecStart=/opt/bin/meertex sync\n")
    fake_systemd(monkeypatch, {
        **UNIT_GONE, "LoadState": "loaded", "FragmentPath": str(unit), "UnitFileState": "disabled",
        "ActiveState": "failed", "SubState": "failed", "NRestarts": "3", "Result": "exit-code",
        "ExecMainStatus": "1", "InactiveEnterTimestamp": "Tue 2026-10-27 22:20:00 UTC",
    })
    code, out, _ = run(capsys, "service", "status")
    assert code == 1 and "failed since" in out and "not enabled at login" in out
    assert "exit-code, status 1" in out and "systemctl --user start meertex.service" in out


def test_service_status_when_not_installed(capsys, monkeypatch):
    fake_systemd(monkeypatch, UNIT_GONE, journal=None)
    code, out, _ = run(capsys, "service", "status")
    assert code == 1 and "not installed" in out and "meertex service install" in out
    assert "No journal files were found." in out
    code, out, _ = run(capsys, "service", "status", "--json")
    s = json.loads(out)
    assert code == 1 and not s["installed"] and s["logs"] == [] and "No journal files" in s["logs_error"]


def test_service_status_without_systemd(capsys, monkeypatch):
    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(service.subprocess, "run", missing)
    code, _, err = run(capsys, "service", "status")
    assert code == 2 and "needs systemd" in err


def test_service_status_follows_the_log(capsys, monkeypatch):
    started = []

    class Journal:
        def __init__(self, argv, **kwargs):
            started.append(self)
            self.argv, self.stopped = argv, False
            self.stdout = iter(json.dumps(e) + "\n" for e in JOURNAL)

        def terminate(self):
            self.stopped = True

        def wait(self):
            pass

    monkeypatch.setattr(service.subprocess, "Popen", Journal)
    code, out, _ = run(capsys, "service", "status", "--follow", "--json", "-n", "2")
    assert code == 0 and [json.loads(line)["pid"] for line in out.splitlines()] == [4242, 4242]
    assert "--follow" in started[0].argv and "--lines=2" in started[0].argv and started[0].stopped


def test_status_lists_checkouts(capsys, tmp_path):
    root = tmp_path / "paper"
    root.mkdir()
    meta = {"project_id": "p1", "name": "paper", "server": SERVER}
    config.write_project(root, meta)
    config.register_checkout(root, meta)
    config.write_json(root / ".meertex" / "status.json", {"connected": True, "last_sync_at": "2026-01-01T00:00:00Z"})
    code, out, _ = run(capsys, "status", "--json")
    states = json.loads(out)
    assert code == 0 and states[0]["name"] == "paper" and states[0]["running"] is False
    assert states[0]["connected"] is False  # nobody holds the lock, so the old status is stale
    code, out, _ = run(capsys, "status")
    assert "not syncing" in out


def test_project_ref_from_a_checkout_folder(fake, capsys, tmp_path, monkeypatch):
    root = tmp_path / "paper"
    (root / "sections").mkdir(parents=True)
    config.write_project(root, {"project_id": "p-123", "name": "paper", "server": SERVER})
    monkeypatch.chdir(root / "sections")
    fake.routes[("GET", "/api/projects/p-123")] = PROJECT
    code, _, _ = run(capsys, "show", ".")
    assert code == 0 and fake.calls[0][1] == "/api/projects/p-123"


# --- Checkouts against a live fake server ----------------------------------------------------


@pytest.fixture
def live(fake, monkeypatch):
    """The fake y-websocket server of test_sync, on a loop in a thread, so the
    command's own asyncio.run can talk to it."""
    from test_sync import FakeServer, free_port

    server = FakeServer(free_port())
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    asyncio.run_coroutine_threadsafe(server.start(), loop).result(5)
    monkeypatch.setenv("MEERTEX_SERVER", server.url)
    monkeypatch.setenv("MEERTEX_TOKEN", "good-token")
    yield server
    asyncio.run_coroutine_threadsafe(server.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def test_new_with_checkout(fake, live, capsys):
    from test_sync import PROJECT as SYNC_PROJECT

    y.put_text(live.doc, "main.tex", "\\documentclass{acmart}\n")
    y.put_text(live.doc, "refs.bib", "% refs\n")
    project = {**PROJECT, "id": SYNC_PROJECT, "url": f"{live.url}/p/{PROJECT['name']}"}
    fake.routes[("POST", "/api/projects")] = (201, project)
    code, out, err = run(capsys, "new", "--title", "Green LLM", "--conference", "ICSE", "--year", "2027",
                         "--checkout", "--json")
    assert code == 0, err
    result = json.loads(out)
    root = Path(result["checkout_dir"])
    assert root == config.papers_dir().absolute() / PROJECT["name"]
    assert (root / "main.tex").read_text() == "\\documentclass{acmart}\n"
    assert config.read_project(root)["project_id"] == SYNC_PROJECT
    assert [c["dir"] for c in config.load_checkouts()] == [str(root.resolve())]
    # A second checkout into the same, now full, folder is refused before anything is created.
    code, _, err = run(capsys, "new", "--title", "Green LLM", "--checkout")
    assert code == 2 and "not empty" in err
    assert all(call[3].get("dry_run") for call in fake.calls[2:])


def test_link_sends_an_existing_folder(fake, live, capsys, tmp_path):
    from test_sync import PROJECT as SYNC_PROJECT

    folder = tmp_path / "old-paper"
    (folder / "figs").mkdir(parents=True)
    (folder / "paper.tex").write_text("\\documentclass{llncs}\nText\n")
    (folder / "paper.pdf").write_bytes(b"%PDF built here")
    (folder / "paper.aux").write_text("aux")
    fake.routes[("POST", "/api/projects")] = (201, {**PROJECT, "id": SYNC_PROJECT, "main_file": "paper.tex"})
    code, out, err = run(capsys, "link", str(folder), "--title", "Old paper", "--conference", "SEAA",
                         "--year", "2027")
    assert code == 0, err
    assert fake.calls[0][3] == {"title": "Old paper", "conference": "SEAA", "year": 2027, "main_file": "paper.tex"}
    assert sorted(y.materialize(live.doc)) == ["paper.tex"]
    assert "sent      1 file\n" in out
    # Linking it again is refused, and so is linking a project that has files.
    assert run(capsys, "link", str(folder), "--project", "x")[0] == 2
    other = tmp_path / "other"
    other.mkdir()
    fake.routes[("GET", "/api/projects/full")] = {**PROJECT, "file_count": 3}
    code, _, err = run(capsys, "link", str(other), "--project", "full")
    assert code == 2 and "has 3 files" in err


def test_word_diff_reads_like_git_word_diff_plain(monkeypatch):
    from meertex import cli, output

    monkeypatch.setattr(output, "use_color", lambda: False)
    porcelain = (
        "diff --git a/main.tex b/main.tex\n"
        "deleted file mode 100644\n"
        "--- a/main.tex\n"
        "+++ b/main.tex\n"
        "@@ -8,1 +8,1 @@\n"
        " Models use a \n"
        "-surprising\n"
        "+staggering\n"
        "  amount of energy.\n"
        "~"
    )
    assert cli._word_lines(porcelain) == [
        "@@ -8,1 +8,1 @@",
        "Models use a [-surprising-]{+staggering+} amount of energy.",
    ]


def test_a_failed_template_import_removes_the_new_project(tmp_path):
    from meertex.api import ApiError

    calls = []

    class FakeApi:
        def post(self, path, json=None, **kwargs):
            calls.append(("post", path))
            raise ApiError(422, "That is not a zip file")

        def delete(self, path, **kwargs):
            calls.append(("delete", path, kwargs.get("params")))

    project = {"id": "p1", "name": "2027-x-kit"}
    with pytest.raises(cli.UsageError, match="That is not a zip file. Nothing was created."):
        cli._import_zip(FakeApi(), project, b"not a zip")
    assert calls[-1] == ("delete", "/api/projects/p1", {"confirm": "2027-x-kit"})


def test_template_zip_is_checked_before_anything_is_made(tmp_path):
    import argparse

    kit = tmp_path / "kit.zip"
    kit.write_bytes(b"PK")
    args = argparse.Namespace(zip=str(kit))
    assert cli._template_zip(args, {}) == b"PK"
    with pytest.raises(cli.UsageError, match="Use one of them"):
        cli._template_zip(args, {"starter": "basic"})
    with pytest.raises(cli.UsageError, match="is not a file"):
        cli._template_zip(argparse.Namespace(zip=str(tmp_path / "missing.zip")), {})
