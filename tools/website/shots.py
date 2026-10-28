"""Screenshots of the real app for the landing page (app/static/landing.html).

The script starts its own server on port 8068 against the database
meertex_website, with a temporary DATA_DIR and the compile service run natively
with this machine's TeX Live. It fills the database with a made-up demo
account, takes the screenshots with Google Chrome through Playwright and writes
them as WebP into app/static/img/screenshots/. tools/website/README.md says how
to run it. Every run starts from an empty database, so it can run again at any
time.

Each screenshot is a function marked with @shot below. To add one, write
another function and use its name in landing.html.
"""

import argparse
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, expect, sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEMO = HERE / "demo"
PYTHON = REPO / ".venv" / "bin" / "python"
UVICORN = REPO / ".venv" / "bin" / "uvicorn"
OUT = REPO / "app" / "static" / "img" / "screenshots"
DEFAULT_DB = "postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_website"

# The demo people. All made up, all on example.com. Lena is the one signed in
# for the screenshots.
PEOPLE = {
    "lena": ("lena@example.com", "Lena Vogt"),
    "tomas": ("tomas@example.com", "Tomás Ibarra"),
    "priya": ("priya@example.com", "Priya Raman"),
    "jonas": ("jonas@example.com", "Jonas Keller"),
}

# Saved widths of every screenshot. The page loads the small one on phones.
WIDTHS = (1920, 960)

# A compile in one of these states is finished. "unavailable" means the server
# found no compile service, which the server's log then explains.
DONE = ("success", "errors", "failure", "timeout", "unavailable")

SHOTS = {}


def shot(name, width=1280, height=800):
    """Register a screenshot. The function gets the run and a page signed in as
    Lena with a viewport of width x height, and leaves the page as it should be
    photographed."""

    def register(fn):
        SHOTS[name] = (fn, width, height)
        return fn

    return register


def day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def due(offset: int, at: str = "23:59") -> str:
    return f"{day(offset)}T{at}"


def wait_until(fn, timeout=60.0, interval=0.5, what="condition"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise RuntimeError(f"Timed out after {timeout:.0f} s waiting for {what} (last: {last!r})")


class Run:
    """The server, the compile service, the demo data and the browser."""

    def __init__(self, args):
        self.args = args
        self.base = f"http://127.0.0.1:{args.port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="meertex-website-", dir=args.tmp))
        self.server = None
        self.compile_service = None
        self.compile_url = f"http://127.0.0.1:{args.compile_port}"
        self.compile_token = secrets.token_hex(16)
        self.session = {}
        self.token = {}
        self.papers = {}
        self.review = None
        # Browser contexts a screenshot opens besides Lena's, closed after it.
        self.extra_contexts = []

    # --- processes -------------------------------------------------------------

    def env(self) -> dict:
        return {**os.environ, "DATABASE_URL": self.args.db, "DATA_DIR": str(self.tmp / "data")}

    def wait_healthy(self, url: str, log: Path, what: str):
        for _ in range(150):
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"{what} did not start, see {log}")

    def reset_database(self):
        """Create the tables and empty them, so every run starts from nothing."""
        name = urlparse(self.args.db.replace("+psycopg", "")).path.lstrip("/")
        if not name.endswith("_website"):
            raise SystemExit(f"Refusing to empty the database {name!r}. Its name must end in _website.")
        code = (
            "from sqlalchemy import text\n"
            "from app.database import Base, engine, init_db\n"
            "init_db()\n"
            "names = ', '.join(t.name for t in Base.metadata.sorted_tables)\n"
            "with engine.begin() as conn:\n"
            "    conn.execute(text(f'TRUNCATE {names} RESTART IDENTITY CASCADE'))\n"
        )
        subprocess.run([str(PYTHON), "-c", code], cwd=REPO, env=self.env(), check=True)

    def start(self):
        for tool in ("latexmk", "synctex", "latexdiff"):
            if not shutil.which(tool):
                raise SystemExit(f"{tool} is not on the PATH. The compile service needs TeX Live.")
        self.reset_database()
        log = self.tmp / "compile.log"
        with open(log, "a") as out:
            self.compile_service = subprocess.Popen(
                [sys.executable, "-I", str(REPO / "compile" / "service.py")],
                cwd=REPO, stdout=out, stderr=subprocess.STDOUT,
                env={**os.environ, "COMPILE_TOKEN": self.compile_token,
                     "COMPILE_PORT": str(self.args.compile_port), "COMPILE_JOBS_DIR": str(self.tmp / "jobs")},
            )
        self.wait_healthy(self.compile_url, log, "The compile service")
        log = self.tmp / "server.log"
        env = {**self.env(), "BASE_URL": self.base, "SMTP_HOST": "",
               "COMPILE_URL": self.compile_url, "COMPILE_TOKEN": self.compile_token}
        with open(log, "a") as out:
            self.server = subprocess.Popen(
                [str(UVICORN), "app.main:app", "--host", "127.0.0.1", "--port", str(self.args.port),
                 "--ws-max-size", "100000000"],
                cwd=REPO, env=env, stdout=out, stderr=subprocess.STDOUT,
            )
        self.wait_healthy(self.base, log, "The server")

    def stop(self):
        for proc, sig in ((self.server, signal.SIGINT), (self.compile_service, signal.SIGTERM)):
            if proc and proc.poll() is None:
                proc.send_signal(sig)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.args.keep_tmp:
            print(f"Kept the server's data and logs in {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # --- people ------------------------------------------------------------------

    def make_people(self):
        """Accounts with a session and an API token, made in the database the way
        a sign-in makes them (tools/editor/tests/test_editor.py does the same)."""
        code = (
            "import sys\n"
            "from app.database import SessionLocal\n"
            "from app.security import create_session, get_or_create_user, new_api_token\n"
            "with SessionLocal() as db:\n"
            "    for arg in sys.argv[1:]:\n"
            "        email, name = arg.split('|')\n"
            "        u = get_or_create_user(db, email)\n"
            "        u.name = name\n"
            "        u.timezone = 'Europe/Berlin'\n"
            "        u.api_token = u.api_token or new_api_token()\n"
            "        db.commit()\n"
            "        print(email, create_session(db, u), u.api_token)\n"
        )
        out = subprocess.run([str(PYTHON), "-c", code, *[f"{e}|{n}" for e, n in PEOPLE.values()]],
                             cwd=REPO, env=self.env(), check=True, capture_output=True, text=True).stdout
        made = {line.split()[0]: line.split()[1:] for line in out.strip().splitlines()}
        for who, (email, _) in PEOPLE.items():
            self.session[who], self.token[who] = made[email]

    def api(self, who: str) -> httpx.Client:
        return httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {self.token[who]}"}, timeout=60)

    # --- demo data -----------------------------------------------------------------

    def seed(self):
        with self.api("lena") as c:
            self.seed_green_llm(c)
            self.seed_carbon_ci(c)
            self.seed_wasm(c)
            self.seed_energy_repro(c)
            self.seed_metrics(c)
            self.seed_energy_label(c)
            self.seed_plan(c)
            self.wait_for_versions(c)
        self.seed_review()

    def create(self, c, key: str, body: dict, members: dict | None = None) -> dict:
        r = c.post("/api/projects", json=body)
        r.raise_for_status()
        project = r.json()
        for who, role in (members or {}).items():
            c.post(f"/api/projects/{project['id']}/members",
                   json={"email": PEOPLE[who][0], "role": role}).raise_for_status()
        self.papers[key] = project
        return project

    @staticmethod
    def put_files(c, project: dict, files: dict):
        for path, content in files.items():
            body = content.encode() if isinstance(content, str) else content
            c.put(f"/api/projects/{project['id']}/files/{path}", content=body).raise_for_status()

    @staticmethod
    def answer(c, project: dict, kind: str, answer: str, **extra) -> dict:
        p = c.get(f"/api/projects/{project['id']}").json()
        date_id = next(d["id"] for d in p["dates"] if d["kind"] == kind)
        r = c.post(f"/api/projects/{project['id']}/dates/{date_id}/answer", json={"answer": answer, **extra})
        r.raise_for_status()
        return r.json()

    def answer_yes(self, c, project: dict, kind: str, note: str = "") -> dict:
        return self.answer(c, project, kind, "yes", version={"at": "now", "note": note})

    def seed_green_llm(self, c):
        """The paper in the editor: real LaTeX, a .bib, a coauthor and a reviewer.
        Its submission deadline is in two days, so its countdown is red."""
        p = self.create(c, "green_llm", {
            "title": "Measuring the Energy of LLM Inference on Shared GPUs",
            "paper": "green-llm",
            "conference": "EuroSys",
            "conference_name": "European Conference on Computer Systems",
            "year": 2027,
            "track": "Fall deadline",
            "cfp_url": "https://eurosys.example.org/2027/cfp",
            "conference_url": "https://eurosys.example.org/2027",
            "submission_url": "https://eurosys27-fall.example.org",
            "location": "Example City",
            "publisher": "ACM",
            "page_limit": "12 pages, references not counted",
            "template": "acmart sigconf",
            "review_model": "double-blind",
            "notes": "- Double-blind. Cite our own work in the third person.\n"
                     "- Artifact evaluation is optional and starts after the notification.",
            "labels": ["green-software", "gpu"],
            "folder": "2027",
            "dates": [
                {"kind": "abstract", "due_at": due(-5), "tz": "AoE"},
                {"kind": "submission", "due_at": due(2), "tz": "AoE"},
                {"kind": "rebuttal_start", "date": day(62), "tz": "AoE"},
                {"kind": "rebuttal_end", "date": day(66), "tz": "AoE"},
                {"kind": "notification", "date": day(92), "tz": "AoE"},
                {"kind": "camera_ready", "date": day(134), "tz": "AoE"},
                {"kind": "conference_start", "date": day(170), "tz": "Europe/Berlin"},
                {"kind": "conference_end", "date": day(173), "tz": "Europe/Berlin"},
            ],
        }, members={"tomas": "editor", "priya": "reviewer"})
        root = DEMO / "green-llm"
        self.put_files(c, p, {f.relative_to(root).as_posix(): f.read_bytes()
                              for f in sorted(root.rglob("*")) if f.is_file()})
        self.answer(c, p, "abstract", "yes")

    def seed_carbon_ci(self, c):
        """Handed in, the rebuttal deadline passed and nobody said yet whether a
        rebuttal went out. The overview screenshot shows its question."""
        p = self.create(c, "carbon_ci", {
            "title": "Carbon-Aware Scheduling for Continuous Integration",
            "paper": "carbon-ci",
            "conference": "ICSE",
            "conference_name": "International Conference on Software Engineering",
            "year": 2027,
            "track": "Research Track",
            "cfp_url": "https://icse.example.org/2027/research-track",
            "conference_url": "https://icse.example.org/2027",
            "submission_url": "https://icse2027.example.org",
            "location": "Example City",
            "publisher": "IEEE",
            "page_limit": "10 pages + 2 pages of references",
            "template": "IEEEtran conference",
            "review_model": "double-blind",
            "notes": "- Double-blind. No author names, cite own work in the third person.\n"
                     "- Data availability statement required.\n"
                     "- Rebuttal is limited to 750 words.",
            "labels": ["green-software", "ci"],
            "folder": "2027",
            "dates": [
                {"kind": "abstract", "due_at": due(-102), "tz": "AoE"},
                {"kind": "submission", "due_at": due(-95), "tz": "AoE"},
                {"kind": "rebuttal_start", "date": day(-9), "tz": "AoE"},
                {"kind": "rebuttal_end", "date": day(-2), "tz": "AoE"},
                {"kind": "notification", "date": day(4), "tz": "AoE"},
                {"kind": "camera_ready", "date": day(52), "tz": "AoE"},
                {"kind": "conference_start", "date": day(180), "tz": "America/Sao_Paulo"},
                {"kind": "conference_end", "date": day(186), "tz": "America/Sao_Paulo"},
            ],
        }, members={"jonas": "editor"})
        self.put_files(c, p, {"main.tex": (DEMO / "carbon-ci" / "main.tex").read_bytes()})
        self.answer(c, p, "abstract", "yes")
        self.answer_yes(c, p, "submission", note="Handed in on HotCRP, submission 318.")
        # The notification moved by five days, so the overview shows "moved from".
        p2 = c.get(f"/api/projects/{p['id']}").json()
        notification = next(d for d in p2["dates"] if d["kind"] == "notification")
        c.patch(f"/api/projects/{p['id']}/dates/{notification['id']}",
                json={"date": day(9), "tz": "AoE"}).raise_for_status()

    def seed_wasm(self, c):
        """Accepted, with the camera-ready version due in twelve days."""
        p = self.create(c, "wasm", {
            "title": "Energy Profiles of WebAssembly Runtimes",
            "paper": "wasm-energy",
            "conference": "e-Energy",
            "conference_name": "International Conference on Future and Sustainable Energy Systems",
            "year": 2027,
            "track": "Full papers",
            "publisher": "ACM",
            "template": "acmart sigconf",
            "review_model": "double-blind",
            "labels": ["green-software", "wasm"],
            "folder": "2027",
            "starter": "basic",
            "dates": [
                {"kind": "abstract", "due_at": due(-121), "tz": "AoE"},
                {"kind": "submission", "due_at": due(-114), "tz": "AoE"},
                {"kind": "notification", "date": day(-6), "tz": "AoE"},
                {"kind": "camera_ready", "date": day(12), "tz": "AoE"},
                {"kind": "conference_start", "date": day(222), "tz": "Europe/Berlin"},
            ],
        }, members={"tomas": "editor"})
        self.answer(c, p, "abstract", "yes")
        self.answer_yes(c, p, "submission")
        self.answer(c, p, "notification", "accepted")

    def seed_energy_repro(self, c):
        """A journal paper in revision. The live text is the revision, and the
        Compare view shows the latexdiff PDF against the submission."""
        p = self.create(c, "energy_repro", {
            "title": "Reproducible Energy Measurements for Software Engineering Research",
            "paper": "energy-repro",
            "conference": "TOSEM",
            "conference_name": "ACM Transactions on Software Engineering and Methodology",
            "year": 2027,
            "publisher": "ACM",
            "template": "acmart acmsmall",
            "review_model": "double-blind",
            "labels": ["methods"],
            "folder": "Journals",
            "dates": [
                {"kind": "submission", "due_at": due(-151), "tz": "AoE"},
                {"kind": "notification", "date": day(-21), "tz": "AoE"},
            ],
        })
        self.put_files(c, p, {"main.tex": (DEMO / "energy-repro" / "main.tex").read_bytes()})
        self.answer_yes(c, p, "submission")
        self.answer(c, p, "notification", "revision")
        c.post(f"/api/projects/{p['id']}/dates", json={"kind": "revision", "date": day(23), "tz": "AoE"}).raise_for_status()
        self.put_files(c, p, {"main.tex": (DEMO / "energy-repro" / "revision" / "main.tex").read_bytes()})

    def seed_metrics(self, c):
        """Rejected at one venue and resubmitted to the next one as a fork."""
        p = self.create(c, "metrics_fse", {
            "title": "Green Software Metrics in Practice",
            "paper": "green-metrics",
            "conference": "FSE",
            "conference_name": "International Conference on the Foundations of Software Engineering",
            "year": 2026,
            "publisher": "ACM",
            "labels": ["green-software"],
            "folder": "2026",
            "starter": "basic",
            "dates": [
                {"kind": "submission", "due_at": due(-212), "tz": "AoE"},
                {"kind": "notification", "date": day(-120), "tz": "AoE"},
            ],
        })
        self.answer_yes(c, p, "submission")
        self.answer(c, p, "notification", "rejected")
        r = c.post(f"/api/projects/{p['id']}/fork", json={
            "title": "Green Software Metrics in Practice",
            "paper": "green-metrics",
            "conference": "ESEM",
            "conference_name": "International Symposium on Empirical Software Engineering and Measurement",
            "year": 2027,
            "publisher": "ACM",
            "labels": ["green-software"],
            "folder": "2027",
            "dates": [
                {"kind": "abstract", "due_at": due(38), "tz": "AoE"},
                {"kind": "submission", "due_at": due(45), "tz": "AoE"},
                {"kind": "notification", "date": day(110), "tz": "AoE"},
            ],
        })
        r.raise_for_status()
        self.papers["metrics_esem"] = r.json()

    def seed_energy_label(self, c):
        """Accepted last year and published. Nothing is due any more."""
        p = self.create(c, "energy_label", {
            "title": "An Energy Label for Mobile Apps",
            "paper": "energy-label",
            "conference": "ICT4S",
            "conference_name": "International Conference on ICT for Sustainability",
            "year": 2026,
            "publisher": "IEEE",
            "labels": ["green-software", "mobile"],
            "folder": "2026",
            "starter": "basic",
            "dates": [
                {"kind": "submission", "due_at": due(-320), "tz": "AoE"},
                {"kind": "notification", "date": day(-250), "tz": "AoE"},
                {"kind": "camera_ready", "date": day(-222), "tz": "AoE"},
                {"kind": "conference_start", "date": day(-160), "tz": "Europe/Stockholm"},
            ],
        })
        self.answer_yes(c, p, "submission")
        self.answer(c, p, "notification", "accepted")
        self.answer_yes(c, p, "camera_ready")

    def seed_plan(self, c):
        """A venue planned before the topic is clear. Its writing window starts
        today and overlaps the EuroSys, TOSEM and ESEM ones, so the timeline's
        writing load turns red."""
        self.create(c, "ict4s_plan", {
            "planned": True,
            "conference": "ICT4S",
            "conference_name": "International Conference on ICT for Sustainability",
            "year": 2027,
            "publisher": "IEEE",
            "labels": ["green-software"],
            "folder": "2027",
            "dates": [
                {"kind": "submission", "due_at": due(30), "tz": "AoE"},
                {"kind": "notification", "date": day(85), "tz": "AoE"},
                {"kind": "camera_ready", "date": day(110), "tz": "AoE"},
                {"kind": "conference_start", "date": day(200), "tz": "Europe/Stockholm"},
                {"kind": "conference_end", "date": day(203), "tz": "Europe/Stockholm"},
            ],
        })

    def wait_for_versions(self, c):
        """Every version is compiled in the background. Wait for all of them."""

        def compiled():
            waiting = []
            for p in self.papers.values():
                for v in c.get(f"/api/projects/{p['id']}").json()["versions"]:
                    if (v.get("compiled") or {}).get("status") not in DONE:
                        waiting.append(f"{p['name']} {v['tag']}")
            return not waiting or None

        wait_until(compiled, timeout=300, interval=1, what="the versions' PDFs")

    def seed_review(self):
        """Priya reviews the EuroSys draft. Lena applies her suggestion, rewrites a
        caption and answers, Tomás answers too, and Priya accepts the change.
        Threads are listed in file order, so the suggestion in main.tex and the
        resolved caption come first on the Conversation view."""
        p = self.papers["green_llm"]
        url = f"/api/projects/{p['id']}"
        root = DEMO / "green-llm"

        def span(path, needle):
            text = (root / path).read_text()
            start = text.index(needle)
            return {"path": path, "start": start, "end": start + len(needle)}

        with self.api("priya") as c:
            review = c.post(f"{url}/reviews", json={"ref": "live"}).json()
            rid = review["id"]

            def thread(**body):
                r = c.post(f"{url}/threads", json={**body, "review_id": rid})
                r.raise_for_status()
                return r.json()

            lines = (root / "main.tex").read_text().splitlines()
            line = next(i for i, text in enumerate(lines, 1) if text.startswith("We present"))
            tools = thread(path="main.tex", line_start=line,
                           body="Name the tools here. Reviewers will ask which traces and which sensor.",
                           suggestion=lines[line - 1].replace("from kernel traces and the device's power readings",
                                                              "from CUPTI kernel traces and the power readings of NVML"))
            caption = thread(**span("sections/evaluation.tex", "The same two requests under three load levels."),
                             body="The caption should say what the two bars are.")
            claim = thread(**span("sections/evaluation.tex",
                                  "Figure~\\ref{fig:load} shows why a per-request figure needs the load around it."),
                           body="Does this hold for all four models, or only for the one in the figure?")
            opening = thread(**span("sections/introduction.tex", "now one of the largest uses of data-centre GPUs"),
                             body="Can we put a number on this? A share of GPU hours from the census would make "
                                  "the opening much stronger.")
            c.post(f"{url}/reviews/{rid}/submit", json={
                "verdict": "changes_requested",
                "summary": "A clear paper with a result people will use. Four small things before the deadline.",
            }).raise_for_status()

        with self.api("lena") as c:
            c.post(f"{url}/threads/{tools['id']}/apply", json={}).raise_for_status()
            evaluation = (root / "sections/evaluation.tex").read_text().replace(
                "The same two requests under three load levels.",
                "Energy of one short prompt and one long generation under three load levels.")
            c.put(f"{url}/files/sections/evaluation.tex", content=evaluation.encode()).raise_for_status()
            c.post(f"{url}/threads/{caption['id']}/resolve",
                   json={"note": "The caption now names both requests."}).raise_for_status()
            c.post(f"{url}/threads/{opening['id']}/comments",
                   json={"body": "Good idea. The census reports 38 % of GPU hours for inference. I will add it."},
                   ).raise_for_status()
        with self.api("tomas") as c:
            c.post(f"{url}/threads/{claim['id']}/comments",
                   json={"body": "It holds for all four models. I will add a sentence that says so."},
                   ).raise_for_status()
        with self.api("priya") as c:
            c.post(f"{url}/threads/{tools['id']}/accept", json={}).raise_for_status()
        self.review = review

    # --- the browser --------------------------------------------------------------

    def context(self, browser, who="lena", width=1280, height=800):
        ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2,
                                  timezone_id="Europe/Berlin", locale="en-US")
        ctx.add_cookies([{"name": "meertex_session", "value": self.session[who], "url": self.base}])
        return ctx

    def url(self, key: str, rest: str = "") -> str:
        return f"{self.base}/p/{self.papers[key]['name']}{rest}"


def save(raw: Path, name: str, out: Path) -> list[Path]:
    """The PNG from the browser as WebP, once per width in WIDTHS."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for width in WIDTHS:
        target = out / (f"{name}.webp" if width == WIDTHS[0] else f"{name}-{width}.webp")
        subprocess.run(["magick", str(raw), "-resize", f"{width}x", "-strip", "-quality", "82",
                        "-define", "webp:method=6", str(target)], check=True)
        written.append(target)
    return written


def settle(page: Page, ms: int = 600):
    """Let fonts, transitions and late paints finish."""
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(ms)


# --- the screenshots ----------------------------------------------------------------


@shot("papers")
def papers(run: Run, page: Page, browser):
    """The paper list: folders, countdowns, outcomes and the question waiting."""
    page.goto(f"{run.base}/")
    expect(page.locator(".next-date-when").first).to_be_visible(timeout=20000)
    expect(page.get_by_text("Did you send a rebuttal?").first).to_be_visible()
    settle(page)


@shot("overview")
def overview(run: Run, page: Page, browser):
    """A paper's dates as a timeline, its versions and the question banner."""
    page.goto(run.url("carbon_ci"))
    expect(page.get_by_text("Did you send a rebuttal?").first).to_be_visible(timeout=20000)
    settle(page)


@shot("timeline")
def timeline(run: Run, page: Page, browser):
    """Every paper on the timeline with its writing window, review and
    camera-ready, the plan drawn dashed, and the writing load where three
    windows overlap. No hover card, it would cover the overlapping bars."""
    page.goto(f"{run.base}/timeline")
    expect(page.locator(".tl-row").first).to_be_visible(timeout=20000)
    expect(page.locator(".tl-load-seg.is-many").first).to_be_visible()
    settle(page)


@shot("pdfdiff")
def pdfdiff(run: Run, page: Page, browser):
    """The Compare view of the journal paper. It shows the latexdiff PDF of
    the submission against the revision, scrolled to the marked abstract."""
    page.goto(run.url("energy_repro", "/compare?from=submission&to=live&pdf=1"))
    expect(page.locator(".pdf-box .pdfview-page").first).to_be_visible(timeout=240000)
    settle(page, 1000)
    page.evaluate("""() => {
        const first = document.querySelector('.pdf-box .pdfview-page');
        first.closest('.pdfview').scrollTop = first.offsetTop + first.offsetHeight * 0.17;
    }""")
    settle(page, 1500)


@shot("versions")
def versions(run: Run, page: Page, browser):
    """The Versions tab with the compiled PDF of the submission."""
    page.goto(run.url("carbon_ci", "/versions"))
    card = page.locator(".release").first
    expect(card).to_be_visible(timeout=20000)
    preview = card.get_by_role("button", name="Preview")
    if preview.count():
        preview.click()
        expect(card.locator(".pdfview-page").first).to_be_visible(timeout=30000)
    settle(page, 1000)


@shot("review", width=1440, height=900)
def review(run: Run, page: Page, browser):
    """Priya's review of the EuroSys draft on the Conversation view, scrolled to
    the review's head. Needs the review pages (app/static/js/pages/review.js)."""
    page.goto(run.url("green_llm", f"/reviews/{run.review['id']}"))
    expect(page.locator(".rv-thread").first).to_be_visible(timeout=20000)
    settle(page, 800)
    page.evaluate("""() => {
        const head = document.querySelector('.rv-head');
        window.scrollTo(0, head.getBoundingClientRect().top + window.scrollY - 16);
    }""")
    settle(page, 600)


@shot("review-files")
def review_files(run: Run, page: Page, browser):
    """The review's Files view on the introduction. The source has an open
    thread under its line, and the PDF of the reviewed commit is next to it."""
    page.goto(run.url("green_llm", f"/reviews/{run.review['id']}/files?path=sections/introduction.tex"))
    expect(page.locator(".rv-thread").first).to_be_visible(timeout=20000)
    expect(page.locator(".pdfview-page").first).to_be_visible(timeout=120000)
    settle(page, 800)
    # The view fills the window like the editor, and the source scrolls on its
    # own. Scroll it so the commented line stays on top and the open thread and
    # its replies fill the column.
    page.evaluate("""() => {
        const scroller = document.querySelector('.rv-code-scroll');
        const thread = document.querySelector('.rv-thread');
        scroller.scrollTop += thread.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 94;
    }""")
    settle(page, 1200)


def put_cursor(page: Page, needle: str) -> int:
    """Put the cursor at the end of the first line holding needle, without
    scrolling, and return the line's number."""
    return page.evaluate(
        """async (needle) => {
            const { EditorView } = await import('/static/vendor/editor/editor.js');
            const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
            const doc = view.state.doc;
            for (let i = 1; i <= doc.lines; i++) {
                const line = doc.line(i);
                if (line.text.includes(needle)) {
                    view.dispatch({ selection: { anchor: line.to } });
                    view.focus();
                    return i;
                }
            }
            throw new Error(`no line holds ${needle}`);
        }""",
        needle,
    )


@shot("editor", width=1440, height=900)
def editor(run: Run, page: Page, browser):
    """The editor with the PDF, a coauthor's cursor and the \\cite completion."""
    path = "sections/introduction.tex"
    # Tomás has the same file open, so his cursor shows in Lena's editor.
    other = run.context(browser, "tomas", 1440, 900)
    run.extra_contexts.append(other)
    tomas = other.new_page()
    tomas.goto(run.url("green_llm", f"/edit/{path}"))
    expect(tomas.locator(".editor-status")).to_have_text("Connected", timeout=20000)
    put_cursor(tomas, "Production deployments rarely look like that.")

    page.goto(run.url("green_llm", f"/edit/{path}"))
    expect(page.locator(".editor-status")).to_have_text("Connected", timeout=20000)
    expect(page.locator(".pdf-status")).to_have_text(re.compile(r"^Compiled in"), timeout=120000)
    expect(page.locator(".pdfview-page").first).to_be_visible()
    put_cursor(page, "reports one number for everything that runs on the board")
    page.evaluate("window.scrollTo(0, 0)")
    page.keyboard.type(" Pricing inference by energy needs the same figures~\\cite{")
    expect(page.locator(".cm-tooltip-autocomplete")).to_be_visible(timeout=5000)
    # The name over a remote cursor shows while the mouse is on it.
    caret = page.locator(".cm-ySelectionCaret").first
    if caret.count():
        caret.hover()
    settle(page, 700)


@shot("references", width=1440, height=900)
def references(run: Run, page: Page, browser):
    """The References tab of the EuroSys draft after a real check, so it needs
    the network. The five real works in demo/green-llm/refs.bib check out,
    one of them with a wrong year, and Lena marked the first as checked. The
    made-up ones are in no index, and the first of them is open."""
    paper = run.papers["green_llm"]
    base = f"/api/projects/{paper['id']}/references"
    with run.api("lena") as c:
        r = c.post(f"{base}/check", json={})
        r.raise_for_status()
        run_id = r.json()["id"]

        def finished():
            check = c.get(f"{base}/check/{run_id}").json()
            return check if check["status"] != "running" else None

        check = wait_until(finished, timeout=600, interval=2, what="the reference check")
        if check["status"] != "done":
            raise RuntimeError(f"The reference check {check['status']}: {check.get('error')}")
        c.post(f"{base}/strubell2019energy/checked").raise_for_status()
    page.goto(run.url("green_llm", "/references"))
    row = page.locator(".rf-entry", has=page.locator(".rf-key", has_text="kessler2025inference"))
    expect(row).to_be_visible(timeout=20000)
    expect(page.locator(".rf-entry .Label", has_text="Checked")).to_be_visible()
    row.locator(".rf-toggle").click()
    expect(row.locator(".rf-finding.is-error")).to_be_visible()
    page.evaluate("() => window.scrollTo(0, 0)")
    settle(page, 800)

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("only", nargs="*", help=f"take only these screenshots ({', '.join(SHOTS)})")
    parser.add_argument("--port", type=int, default=8068, help="the server's port (8068)")
    parser.add_argument("--compile-port", type=int, default=8078, help="the compile service's port (8078)")
    parser.add_argument("--db", default=os.environ.get("MEERTEX_WEBSITE_DB", DEFAULT_DB),
                        help="the database, emptied first. Its name must end in _website")
    parser.add_argument("--out", default=str(OUT), help="where the WebP files go")
    parser.add_argument("--raw", help="also keep the browser's PNGs in this folder")
    parser.add_argument("--tmp", help="the folder for the server's data and logs (default: the system's)")
    parser.add_argument("--keep-tmp", action="store_true", help="keep the server's data and logs")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()
    unknown = [n for n in args.only if n not in SHOTS]
    if unknown:
        parser.error(f"no such screenshot: {', '.join(unknown)}")

    run = Run(args)
    raw = Path(args.raw) if args.raw else run.tmp / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    try:
        t0 = time.monotonic()
        run.start()
        run.make_people()
        run.seed()
        print(f"seeded {len(run.papers)} papers in {time.monotonic() - t0:.0f} s", flush=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=not args.headed)
            for name, (fn, width, height) in SHOTS.items():
                if args.only and name not in args.only:
                    continue
                ctx = run.context(browser, "lena", width, height)
                page = ctx.new_page()
                fn(run, page, browser)
                png = raw / f"{name}.png"
                page.screenshot(path=str(png))
                for target in save(png, name, Path(args.out)):
                    print(f"{target.relative_to(REPO) if target.is_relative_to(REPO) else target}"
                          f"  {target.stat().st_size // 1024} KB", flush=True)
                ctx.close()
                while run.extra_contexts:
                    run.extra_contexts.pop().close()
            browser.close()
    finally:
        run.stop()


if __name__ == "__main__":
    main()
