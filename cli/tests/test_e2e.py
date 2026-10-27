"""End to end, with the real server as a subprocess and the real ``meertex`` command.

Skipped unless MEERTEX_TEST_DB is set. The database belongs to these tests,
and every run empties it first. Run them like this.

    MEERTEX_TEST_DB=postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_cli \\
        .venv/bin/pytest -q cli/tests/test_e2e.py

The server runs with uvicorn on port 8062 (MEERTEX_E2E_PORT moves it) and
DATA_DIR in a temporary folder.
Without SMTP_HOST it prints its mails, which is where the sign-in code is read.
The CLI runs as ``python -m meertex`` with the conftest's temporary
MEERTEX_HOME, MEERTEX_PAPERS and XDG_CONFIG_HOME.
"""

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MEERTEX_TEST_DB"), reason="MEERTEX_TEST_DB not set")

REPO = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("MEERTEX_E2E_PORT") or 8062)
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable
EMAIL = "e2e@example.com"
PNG = b"\x89PNG\r\n\x1a\n" + hashlib.sha256(b"plot").digest() * 64


def until(condition, timeout: float = 30.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if condition():
                return
        except (OSError, ValueError, httpx.HTTPError):
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


class Server:
    """uvicorn with the app on PORT, restartable on the same database."""

    def __init__(self, tmp: Path):
        self.log_path = tmp / "server.log"
        self.env = {
            **os.environ,
            "DATABASE_URL": os.environ["MEERTEX_TEST_DB"],
            # No mails from the reminder loop in a test server.
            "REMINDERS_ENABLED": "false",
            "DATA_DIR": str(tmp / "data"),
            "BASE_URL": BASE,
            "SECRET_KEY": "e2e-secret",
            "SMTP_HOST": "",
            "LOGIN_RATE_MAX": "1000",
            "VERIFY_RATE_MAX": "1000",
            "PYTHONUNBUFFERED": "1",
        }
        self.proc: subprocess.Popen | None = None

    def reset_database(self) -> None:
        script = (
            "from sqlalchemy import text\n"
            "from app.database import Base, engine, init_db\n"
            "init_db()\n"
            "names = ', '.join(t.name for t in Base.metadata.sorted_tables)\n"
            "with engine.begin() as conn:\n"
            "    conn.execute(text(f'TRUNCATE {names} RESTART IDENTITY CASCADE'))\n"
        )
        subprocess.run([PY, "-c", script], cwd=REPO, env=self.env, check=True, timeout=60)

    def start(self) -> None:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                raise RuntimeError(f"Port {PORT} is taken. Is another server running there?")
        with open(self.log_path, "a") as log:  # the child keeps its own descriptor
            self.proc = subprocess.Popen(
                [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
                cwd=REPO, env=self.env, stdout=log, stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 30
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("The server did not start:\n" + self.log())
            try:
                if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("The server did not answer /healthz:\n" + self.log())
            time.sleep(0.2)

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")[-6000:]

    def login_code(self, email: str) -> str | None:
        pattern = rf"To: {re.escape(email)}\n.*?enter this code on the sign-in screen:\s*([A-Z0-9]{{6}})"
        codes = re.findall(pattern, self.log_path.read_text(errors="replace"), re.DOTALL)
        return codes[-1] if codes else None


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = Server(tmp_path_factory.mktemp("server"))
    srv.reset_database()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture(scope="module")
def token(server) -> str:
    """An account's API token, through the same code flow ``meertex login`` uses."""
    with httpx.Client(base_url=BASE, timeout=30) as c:
        assert c.post("/api/auth/login", json={"email": EMAIL, "client": "cli"}).status_code == 200
        code = server.login_code(EMAIL)
        assert code, server.log()
        assert c.post("/api/auth/verify-code", json={"email": EMAIL, "code": code}).status_code == 200
        return c.post("/api/auth/api-token").json()["api_token"]


@pytest.fixture
def api(token):
    with httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {token}"}, timeout=30) as c:
        yield c


@pytest.fixture
def cli_env(token, tmp_path):
    env = {**os.environ, "MEERTEX_SERVER": BASE, "MEERTEX_TOKEN": token, "NO_COLOR": "1"}
    env.pop("MEERTEX_TEST_DB", None)

    def run(*args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
        r = subprocess.run([PY, "-m", "meertex", *args], env=env, cwd=tmp_path, capture_output=True,
                           text=True, timeout=timeout, check=False)
        if check and r.returncode != 0:
            raise AssertionError(f"meertex {' '.join(args)} exited {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    run.env = env
    return run


class Daemon:
    """``meertex sync DIR`` in the background, like the service runs it."""

    def __init__(self, env: dict, root: Path, log: Path):
        self.root = root
        self.log_path = log
        with open(log, "w") as out:
            self.proc = subprocess.Popen([PY, "-m", "meertex", "sync", str(root)], env=env, cwd=root,
                                         stdout=out, stderr=subprocess.STDOUT)

    def status(self) -> dict:
        return json.loads((self.root / ".meertex" / "status.json").read_text())

    def connected(self) -> bool:
        st = self.status()
        return st.get("connected") is True and st.get("pid") == self.proc.pid

    def stop(self) -> None:
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")


def new_project(cli_env, title: str, *extra: str) -> dict:
    r = cli_env("new", "--title", title, "--conference", "ICSE", "--year", "2027", "--starter", "basic",
                "--json", *extra)
    return json.loads(r.stdout)


def remote_file(api, project_id: str, path: str) -> bytes | None:
    r = api.get(f"/api/projects/{project_id}/files/{path}")
    return r.content if r.status_code == 200 else None


# --- The scenarios ---------------------------------------------------------------------


def test_login_with_the_emailed_code(server, tmp_path):
    env = {**os.environ, "NO_COLOR": "1"}
    for name in ("MEERTEX_TOKEN", "MEERTEX_SERVER", "MEERTEX_TEST_DB"):
        env.pop(name, None)
    email = "login@example.com"
    proc = subprocess.Popen([PY, "-m", "meertex", "login", "--server", BASE, "--email", email], env=env,
                            cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    until(lambda: server.login_code(email), 30, "the sign-in mail")
    out, err = proc.communicate(server.login_code(email) + "\n", timeout=60)
    assert proc.returncode == 0, err
    assert f"Signed in as {email}" in out
    home = Path(os.environ["MEERTEX_HOME"])
    assert (home / "token").stat().st_mode & 0o777 == 0o600
    r = subprocess.run([PY, "-m", "meertex", "projects", "--json"], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0 and json.loads(r.stdout) == []


def test_new_checkout_and_edits_both_ways(server, api, cli_env, tmp_path):
    project = new_project(cli_env, "Green LLM inference", "--date", "submission=2027-01-15", "--checkout")
    pid = project["id"]
    root = Path(project["checkout_dir"])
    assert project["sync"]["ok"]
    assert root.parent == Path(os.environ["MEERTEX_PAPERS"]).absolute()
    assert (root / "main.tex").exists() and (root / "refs.bib").exists()
    assert [c["dir"] for c in json.loads(cli_env("status", "--json").stdout)] == [str(root)]

    # A local edit reaches the server.
    main = (root / "main.tex").read_text() + "\nGrüße aus Köln 😀\n"
    (root / "main.tex").write_text(main)
    cli_env("sync", "--once", str(root))
    assert remote_file(api, pid, "main.tex").decode() == main

    # An API edit reaches the checkout.
    edited = main.replace("Grüße", "Viele Grüße")
    assert api.put(f"/api/projects/{pid}/files/main.tex", content=edited.encode()).status_code == 200
    cli_env("sync", "--once", str(root))
    assert (root / "main.tex").read_text() == edited

    # A blob goes up, and down into a second checkout.
    (root / "fig").mkdir()
    (root / "fig" / "plot.png").write_bytes(PNG)
    cli_env("sync", "--once", str(root))
    assert remote_file(api, pid, "fig/plot.png") == PNG
    second = tmp_path / "second"
    cli_env("checkout", project["name"], str(second))
    assert (second / "fig" / "plot.png").read_bytes() == PNG
    assert (second / "main.tex").read_text() == edited

    # A delete by someone else goes to the trash.
    assert api.delete(f"/api/projects/{pid}/files/refs.bib").status_code == 200
    cli_env("sync", "--once", str(root))
    assert not (root / "refs.bib").exists()
    assert list((root / ".meertex" / "trash").glob("*/refs.bib"))

    # A local rename keeps the file and its id.
    (root / "intro.tex").write_text("Introduction\n")
    cli_env("sync", "--once", str(root))
    before = {f["path"]: f["id"] for f in api.get(f"/api/projects/{pid}/files").json()}
    (root / "sections").mkdir()
    os.rename(root / "intro.tex", root / "sections" / "introduction.tex")
    cli_env("sync", "--once", str(root))
    after = {f["path"]: f["id"] for f in api.get(f"/api/projects/{pid}/files").json()}
    assert "intro.tex" not in after
    assert after["sections/introduction.tex"] == before["intro.tex"]
    assert remote_file(api, pid, "sections/introduction.tex") == b"Introduction\n"

    # The second checkout follows all of that.
    cli_env("sync", "--once", str(second))
    assert not (second / "refs.bib").exists()
    assert (second / "sections" / "introduction.tex").read_text() == "Introduction\n"


def test_two_live_checkouts_converge(server, api, cli_env, tmp_path):
    project = new_project(cli_env, "Two writers")
    pid = project["id"]
    base = "We measure the energy of LLM inference.\n"
    assert api.put(f"/api/projects/{pid}/files/para.tex", content=base.encode()).status_code == 200
    a, b = tmp_path / "a", tmp_path / "b"
    cli_env("checkout", project["name"], str(a))
    cli_env("checkout", project["name"], str(b))
    da = Daemon(cli_env.env, a, tmp_path / "a.log")
    db = Daemon(cli_env.env, b, tmp_path / "b.log")
    try:
        until(lambda: da.connected() and db.connected(), 30, "both clients to connect")
        (a / "para.tex").write_text("We carefully measure the energy of LLM inference.\n")
        (b / "para.tex").write_text("We measure the energy of LLM inference on GPUs.\n")
        merged = "We carefully measure the energy of LLM inference on GPUs.\n"
        until(lambda: (a / "para.tex").read_text() == merged and (b / "para.tex").read_text() == merged,
              30, f"the edits to merge\n{da.log()}\n{db.log()}")
        until(lambda: remote_file(api, pid, "para.tex") == merged.encode(), 10, "the server")
    finally:
        da.stop()
        db.stop()
    assert da.proc.returncode == 0 and db.proc.returncode == 0


def test_offline_edits_sync_after_reconnect(server, api, cli_env, tmp_path):
    project = new_project(cli_env, "Offline work")
    pid = project["id"]
    root = tmp_path / "co"
    cli_env("checkout", project["name"], str(root))
    daemon = Daemon(cli_env.env, root, tmp_path / "daemon.log")
    try:
        until(daemon.connected, 30, "the client to connect")
        server.stop()
        until(lambda: daemon.status().get("connected") is False, 30, "the client to notice")
        text = (root / "main.tex").read_text() + "\nWritten on the train.\n"
        (root / "main.tex").write_text(text)
        until(lambda: daemon.status().get("unsent") is True, 10, "the edit to be queued")
        server.start()
        until(lambda: remote_file(api, pid, "main.tex") == text.encode(), 90,
              f"the offline edit to arrive\n{daemon.log()}")
        until(lambda: daemon.status().get("unsent") is False, 10, "the client to see it sent")
    finally:
        daemon.stop()
        if server.proc is None:
            server.start()


def test_hold_keeps_local_work_until_release(server, api, cli_env, tmp_path):
    project = new_project(cli_env, "Held rewrite")
    pid = project["id"]
    files = f"/api/projects/{pid}/files"
    base = "We measure the energy of LLM inference.\n"
    theirs = "We measure the energy of LLM inference on GPUs.\n"
    both = "We carefully measure the energy of LLM inference on GPUs.\n"
    assert api.put(f"{files}/para.tex", content=base.encode()).status_code == 200
    a, b = tmp_path / "a", tmp_path / "b"
    cli_env("checkout", project["name"], str(a))
    cli_env("checkout", project["name"], str(b))
    daemon = Daemon(cli_env.env, a, tmp_path / "a.log")
    try:
        until(daemon.connected, 30, "the client to connect")
        cli_env("hold", str(a))
        until(lambda: daemon.status().get("held") is True, 10, "the service to see the hold")
        (a / "para.tex").write_text("We carefully measure the energy of LLM inference.\n")
        (a / "rewrite.tex").write_text("A new section, half done.\n")
        until(lambda: daemon.status().get("held_files") == 2, 10, f"the held changes\n{daemon.log()}")

        # A coauthor's edit to the same paragraph still arrives, next to ours.
        assert api.put(f"{files}/para.tex", content=theirs.encode()).status_code == 200
        until(lambda: (a / "para.tex").read_text() == both, 15, f"the coauthor's edit\n{daemon.log()}")

        # Nobody else sees the held work.
        cli_env("sync", "--once", str(b))
        assert (b / "para.tex").read_text() == theirs and not (b / "rewrite.tex").exists()
        assert remote_file(api, pid, "para.tex") == theirs.encode() and remote_file(api, pid, "rewrite.tex") is None
        states = {s["dir"]: s for s in json.loads(cli_env("status", "--json").stdout)}
        assert states[str(a)]["held"] is True and states[str(a)]["held_files"] == 2
        assert states[str(b)]["held"] is False

        # Release sends it all, through the running service.
        released = json.loads(cli_env("release", str(a), "--json").stdout)[0]
        assert released["ok"] and released["via_service"] and released["held_files"] == 2
        until(lambda: remote_file(api, pid, "para.tex") == both.encode(), 15, "the release")
        cli_env("sync", "--once", str(b))
        assert (b / "para.tex").read_text() == both
        assert (b / "rewrite.tex").read_text() == "A new section, half done.\n"
        assert daemon.status().get("held") is False
    finally:
        daemon.stop()
    assert daemon.proc.returncode == 0
