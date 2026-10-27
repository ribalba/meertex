"""A review end to end (DESIGN §13): the real server as a subprocess, two
accounts, and the real ``meertex`` command for both of them.

Skipped unless MEERTEX_TEST_DB is set. Like test_e2e.py it empties that
database first. The server runs on port 8064, so it never collides with
test_e2e.py's server on 8062.
"""

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
PORT = 8064
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable
OWNER = "owner@example.com"
REVIEWER = "reviewer@example.com"
TEXT = "\\section{Intro}\nWir messen die Energie von LLMs.\nGrüße aus Köln 😀.\n"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("review-server")
    log_path = tmp / "server.log"
    env = {
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
    reset = (
        "from sqlalchemy import text\n"
        "from app.database import Base, engine, init_db\n"
        "init_db()\n"
        "names = ', '.join(t.name for t in Base.metadata.sorted_tables)\n"
        "with engine.begin() as conn:\n"
        "    conn.execute(text(f'TRUNCATE {names} RESTART IDENTITY CASCADE'))\n"
    )
    subprocess.run([PY, "-c", reset], cwd=REPO, env=env, check=True, timeout=60)
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError(f"Port {PORT} is taken. Is another server running there?")
    with open(log_path, "a") as log:
        proc = subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
                                cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, log_path.read_text()
            try:
                if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, log_path.read_text()
            time.sleep(0.2)
        yield log_path
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _token(log_path: Path, email: str) -> str:
    """An API token through the code flow ``meertex login`` uses. Without
    SMTP_HOST the server prints its mails, which is where the code is."""
    with httpx.Client(base_url=BASE, timeout=30) as c:
        assert c.post("/api/auth/login", json={"email": email, "client": "cli"}).status_code == 200
        pattern = rf"To: {re.escape(email)}\n.*?enter this code on the sign-in screen:\s*([A-Z0-9]{{6}})"
        deadline = time.monotonic() + 30
        while not (codes := re.findall(pattern, log_path.read_text(errors="replace"), re.DOTALL)):
            assert time.monotonic() < deadline, "no sign-in mail"
            time.sleep(0.1)
        assert c.post("/api/auth/verify-code", json={"email": email, "code": codes[-1]}).status_code == 200
        return c.post("/api/auth/api-token").json()["api_token"]


def _meertex(token: str, cwd: Path):
    env = {**os.environ, "MEERTEX_SERVER": BASE, "MEERTEX_TOKEN": token, "NO_COLOR": "1"}
    env.pop("MEERTEX_TEST_DB", None)

    def run(*args: str, stdin: str | None = None) -> str:
        r = subprocess.run([PY, "-m", "meertex", *args], env=env, cwd=cwd, input=stdin, capture_output=True,
                           text=True, timeout=120, check=False)
        assert r.returncode == 0, f"meertex {' '.join(args)} exited {r.returncode}\n{r.stdout}\n{r.stderr}"
        return r.stdout

    return run


def test_a_review_with_the_cli(server, tmp_path):
    owner_token = _token(server, OWNER)
    reviewer_token = _token(server, REVIEWER)
    owner = _meertex(owner_token, tmp_path)
    reviewer = _meertex(reviewer_token, tmp_path)
    with httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {owner_token}"}, timeout=30) as api:
        p = api.post("/api/projects", json={"name": "paper", "title": "Energy paper"}).json()
        assert api.put(f"/api/projects/{p['id']}/files/main.tex", content=TEXT.encode()).status_code == 200
        r = api.post(f"/api/projects/{p['id']}/members", json={"email": REVIEWER, "role": "reviewer"})
        assert r.status_code == 201

        # The reviewer reviews: a pending review, two comments, the verdict.
        started = json.loads(reviewer("review", "paper", "start", "--json"))
        assert started["state"] == "pending"
        t = json.loads(reviewer("comment", "paper", "main.tex:2", "Which energy?", "--review",
                                "--suggestion", "Wir messen die GPU-Energie von LLMs.", "--json"))
        assert (t["pending"], t["quote"], t["review_id"]) == (True, "Wir messen die Energie von LLMs.", started["id"])
        reviewer("comment", "paper", "main.tex:3", "-", "--review", stdin="Schöner Gruß.\nZweite Zeile.")
        assert json.loads(owner("threads", "--json")) == []
        out = reviewer("review", "paper", "submit", "--verdict", "changes-requested", "--summary", "Two points.")
        assert "with 2 threads" in out

        # The owner sees every open thread, across projects, and works on them.
        threads = json.loads(owner("threads", "--json"))
        assert [x["project"]["name"] for x in threads] == ["paper", "paper"]
        assert [x["line_start"] for x in threads] == [2, 3]
        listing = owner("threads")
        short = t["id"][:8]
        assert f"{short}  open  main.tex:2  {REVIEWER}  review {started['id'][:8]}" in listing
        assert "    > Wir messen die Energie von LLMs." in listing
        assert "      + Wir messen die GPU-Energie von LLMs." in listing
        assert "    Zweite Zeile." in listing

        owner("reply", "paper", short, "Stimmt, nur die GPU.")
        assert "Applied the suggestion to main.tex:2" in owner("apply", "paper", short)
        live = api.get(f"/api/projects/{p['id']}/files/main.tex").text
        assert live == TEXT.replace("die Energie", "die GPU-Energie")
        other = threads[1]["id"][:8]
        api.put(f"/api/projects/{p['id']}/files/main.tex", content=live.replace("Grüße aus Köln", "Grüße aus Bonn"))
        owner("resolve", "paper", other, "--note", "Bonn ist schöner.")

        # The reviewer looks at what happened and accepts one, reopens the other.
        done = json.loads(reviewer("thread", "paper", short, "--json"))
        assert done["state"] == "resolved"
        assert [c["kind"] for c in done["comments"]] == ["comment", "comment", "applied"]
        assert "-Energie" in done["resolution"]["diff"] and "+GPU-Energie" in done["resolution"]["diff"]
        reviewer("accept", "paper", short)
        out = reviewer("reopen", "paper", other, "--note", "Köln bitte lassen.")
        assert "now open" in out
        reviews = json.loads(owner("reviews", "paper", "--json"))
        assert reviews[0]["counts"] == {"open": 1, "resolved": 0, "accepted": 1}
        assert "changes requested" in owner("reviews", "paper")

    log = server.read_text(errors="replace")
    assert f"To: {OWNER}\nSubject: meertex · {REVIEWER} reviewed Energy paper" in log
    assert f"{BASE}/p/paper/reviews/{started['id']}#thread-{t['id']}" in log
