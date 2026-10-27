"""End-to-end tests of the References tab and the reference problems in the editor.

The References tab (app/static/js/pages/references.js), its counter in the
paper's tabs (pages/project.js) and the reference check's errors and warnings
in the editor's problems list and on their lines (pages/editor-pdf.js).
Google Chrome through Playwright against a real server and a real database.
tools/editor/README.md says how to run it.

The server runs with REFERENCES_NETWORK=false and without ANTHROPIC_API_KEY,
so nothing goes to the internet. What a check run would have stored is put
into reference_results directly. The tests that need a server with lookups
(the check buttons, the progress of a run, adding an entry) change the
report's answer in the browser with Playwright's routes and answer the
check's routes there. The tests run in order and build on each other. A run
prints one line per test and exits 1 when one failed.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

REPO = Path(__file__).resolve().parents[3]
PYTHON = REPO / ".venv" / "bin" / "python"
DEFAULT_DB = "postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_references_ui"

MAIN = r"""\documentclass{article}
\begin{document}
Provocation changes visitors \cite{roussou2019}.
Attention is everything \citep{vaswani2017}.
Carbon accounting is solved \citep{fake2023, nothere}.
Software uses energy, and the meters say so \cite{green2020}.
\TeX{} is a typesetting system \cite{knuth:1984/tex}.
\nocite{pending2021}
\bibliography{refs}
\end{document}
"""

BIB = r"""@inproceedings{roussou2019,
  title = {Transformation through Provocation?},
  author = {Roussou, Maria and Perry, Sara},
  booktitle = {Proceedings of CHI '19},
  year = {2019},
  doi = {10.1145/3290605.3300857},
}

@inproceedings{vaswani2017,
  title = {Attention Is All We Need},
  author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and Uszkoreit, Jakob},
  booktitle = {Advances in Neural Information Processing Systems},
  year = {2017},
}

@article{fake2023,
  title = {Quantum Carbon Accounting for Large Language Models},
  author = {Smith, John and Doe, Jane},
  journal = {Journal of Green Computing},
  year = {2023},
  doi = {10.1145/9999999.9999999},
}

@book{knuth:1984/tex,
  title = {The {\TeX}book},
  author = {Knuth, Donald E.},
  publisher = {Addison-Wesley},
  year = {1984},
}

@article{green2020,
  title = {Measuring the Energy Use of Web Servers},
  author = {Green, Gale and Watt, Walter},
  journal = {Energy Informatics},
  year = {2020},
  doi = {10.1186/s42162-020-00001-x},
}

@misc{pending2021,
  title = {A Note Nobody Looked Up},
  author = {Later, Lou},
  year = {2021},
}

@article{broken,
  title = {Never closed,
"""


def finding(code, level, message, **kw):
    return {"code": code, "level": level, "field": kw.pop("field", None), "message": message,
            "value": kw.pop("value", None), "record_value": kw.pop("record_value", None),
            "source": kw.pop("source", "bibtex"), "url": kw.pop("url", None), **kw}


def record(source, title, authors, year, **kw):
    return {"source": source, "doi": kw.get("doi"), "url": kw.get("url"), "type": kw.get("type"), "title": title,
            "authors": authors, "year": year, "venue": kw.get("venue"), "volume": kw.get("volume"),
            "number": kw.get("number"), "pages": kw.get("pages"), "publisher": kw.get("publisher")}


# What a check run would have stored, by key. pending2021 has nothing.
RESULTS = {
    "roussou2019": {
        "record": record("doi.org", "Transformation through Provocation?", ["Maria Roussou", "Sara Perry"], 2019,
                         doi="10.1145/3290605.3300857", url="https://doi.org/10.1145/3290605.3300857",
                         venue="Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems",
                         pages="1-13", publisher="ACM"),
        "findings": [finding("missing", "info", "The record has the pages, 1-13.", field="pages", record_value="1--13",
                             source="doi.org")],
    },
    "vaswani2017": {
        "record": record("semantic scholar", "Attention Is All You Need",
                         ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit"], 2017, venue="NeurIPS",
                         url="https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776"),
        "findings": [finding("title", "warning", "The title is not the record's, “Attention Is All You Need”.",
                             field="title", value="Attention Is All We Need", record_value="Attention Is All You Need",
                             source="semantic scholar",
                             url="https://www.semanticscholar.org/paper/204e3073870fae3d05bcbc2f6a8e263d9b72e776")],
    },
    "fake2023": {
        "record": None,
        "findings": [finding("doi_not_found", "error",
                             "doi.org does not know the DOI 10.1145/9999999.9999999. The prefix 10.1145 belongs to ACM.",
                             field="doi", value="10.1145/9999999.9999999", source="doi.org")],
    },
    "knuth:1984/tex": {
        "record": record("openlibrary", "The TeXbook", ["Donald E. Knuth"], 1984, publisher="Addison-Wesley"),
        "findings": [],
    },
    "green2020": {
        "record": record("crossref", "Measuring the Energy Use of Web Servers", ["Gale Green", "Walter Watt"], 2020,
                         doi="10.1186/s42162-020-00001-x", venue="Energy Informatics"),
        "findings": [],
        "claims": [finding("claim", "warning",
                           "The source does not support this sentence. The paper measures web servers, not meters.",
                           value="Software uses energy, and the meters say so [green2020].",
                           record_value="We measured the energy use of three web servers under load.",
                           source="claude", path="main.tex", line=6, verdict="not_supported")],
    },
}

SEED = r"""
import json, sys, uuid
from datetime import timedelta
from app.database import SessionLocal
from app.models import ReferenceResult, ReferenceRun, User, utcnow
from app.references import bibtex

data = json.loads(sys.stdin.read())
entries = {e.key: e for e in bibtex.parse(data["bib"], "refs.bib").entries}
now = utcnow()
with SessionLocal() as db:
    by = db.query(User).filter(User.email == data["by"]).one()
    for key, row in data["results"].items():
        claims = row.get("claims")
        db.merge(ReferenceResult(
            project_id=data["project"], fingerprint=bibtex.fingerprint(entries[key]), key=key,
            checked_at=now - timedelta(hours=2), record=row["record"], findings=row["findings"],
            claims=claims, claims_hash="seeded" if claims else "", claims_at=now - timedelta(hours=2) if claims else None))
    db.add(ReferenceRun(id=str(uuid.uuid4()), project_id=data["project"], status="done", claims=True, refresh=False,
                        total=len(data["results"]), done=len(data["results"]), started_at=now - timedelta(hours=2, minutes=1),
                        finished_at=now - timedelta(hours=2), created_by=by.id))
    db.commit()
"""

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


class Env:
    """The server, the people and the paper every test uses."""

    def __init__(self, args):
        self.args = args
        self.port = args.port
        self.base = f"http://127.0.0.1:{self.port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="meertex-references-", dir=args.tmp))
        self.server = None
        self.run = secrets.token_hex(3)
        self.shots = Path(args.shots) if args.shots else None
        self.errors = []
        self.allowed = []  # (url part, status) a test expects the browser to log

    # --- server ----------------------------------------------------------------------

    def start(self):
        env = {
            **os.environ,
            "DATABASE_URL": self.args.db,
            "DATA_DIR": str(self.tmp / "data"),
            "BASE_URL": self.base,
            "SMTP_HOST": "",
            "COMPILE_URL": "",
            "COMPILE_TOKEN": "",
            "REFERENCES_NETWORK": "false",
            "ANTHROPIC_API_KEY": "",
        }
        with open(self.tmp / "server.log", "a") as log:
            self.server = subprocess.Popen(
                [str(REPO / ".venv" / "bin" / "uvicorn"), "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
                cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        for _ in range(100):
            try:
                if httpx.get(f"{self.base}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"The server did not start, see {self.tmp / 'server.log'}")

    def stop(self):
        if self.server and self.server.poll() is None:
            self.server.send_signal(signal.SIGINT)
            try:
                self.server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.server.kill()
        if self.args.keep:
            print(f"Kept {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def python(self, code, *args, stdin=None):
        env = {**os.environ, "DATABASE_URL": self.args.db, "DATA_DIR": str(self.tmp / "data")}
        return subprocess.run([str(PYTHON), "-c", code, *args], cwd=REPO, env=env, check=True, capture_output=True,
                              text=True, input=stdin).stdout

    # --- people and papers -------------------------------------------------------------

    def make_users(self, people):
        """Accounts with a session and an API token, made in the database the
        way a sign-in makes them. Returns {email: (session, token)}."""
        code = (
            "import sys\n"
            "from app.database import SessionLocal\n"
            "from app.security import create_session, get_or_create_user, new_api_token\n"
            "with SessionLocal() as db:\n"
            "    for arg in sys.argv[1:]:\n"
            "        email, name = arg.split('|')\n"
            "        u = get_or_create_user(db, email)\n"
            "        u.name = name\n"
            "        u.api_token = u.api_token or new_api_token()\n"
            "        db.commit()\n"
            "        print(email, create_session(db, u), u.api_token)\n"
        )
        out = self.python(code, *[f"{e}|{n}" for e, n in people])
        return {line.split()[0]: tuple(line.split()[1:]) for line in out.strip().splitlines()}

    def setup(self):
        r = self.run
        self.people = {
            "alice": (f"alice-{r}@example.com", "Alice Andersson"),
            "bob": (f"bob-{r}@example.com", "Bob Brown"),
            "vic": (f"vic-{r}@example.com", "Vic Viewer"),
        }
        made = self.make_users(self.people.values())
        self.session = {k: made[e][0] for k, (e, _) in self.people.items()}
        self.token = {k: made[e][1] for k, (e, _) in self.people.items()}
        self.paper = self.create_paper("alice", f"References test {r}", members={"bob": "editor", "vic": "viewer"})
        self.put("alice", "main.tex", MAIN)
        self.put("alice", "refs.bib", BIB)
        self.seed(self.paper, BIB, RESULTS, by="alice")

    def seed(self, paper, bib, results, by):
        """Store what a check run would have found, and the run itself."""
        self.python(SEED, stdin=json.dumps({"project": paper["id"], "bib": bib, "results": results, "by": self.people[by][0]}))

    def api(self, who):
        return httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {self.token[who]}"}, timeout=60)

    def url(self, rest="", paper=None):
        return f"/api/projects/{(paper or self.paper)['id']}{rest}"

    def create_paper(self, owner, title, members=None):
        with self.api(owner) as c:
            p = c.post("/api/projects", json={"title": title, "conference": "TEST", "year": 2027, "starter": "basic"})
            p.raise_for_status()
            project = p.json()
            for who, role in (members or {}).items():
                c.post(f"/api/projects/{project['id']}/members", json={"email": self.people[who][0], "role": role}).raise_for_status()
        return project

    def file(self, who, path, paper=None):
        with self.api(who) as c:
            r = c.get(self.url(f"/files/{path}", paper))
        return r.text if r.status_code == 200 else None

    def put(self, who, path, body, paper=None):
        with self.api(who) as c:
            c.put(self.url(f"/files/{path}", paper), content=body.encode()).raise_for_status()

    def report(self, who="alice", paper=None):
        with self.api(who) as c:
            r = c.get(self.url("/references", paper))
            r.raise_for_status()
            return r.json()

    # --- browsers ----------------------------------------------------------------------

    def context(self, browser, who, width=1280, height=900):
        ctx = browser.new_context(viewport={"width": width, "height": height})
        ctx.add_cookies([{"name": "meertex_session", "value": self.session[who], "url": self.base}])
        return ctx

    def page(self, ctx, label):
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and not self.expected(m) and self.errors.append(f"{label}: {m.text}"))
        page.on("pageerror", lambda e: self.errors.append(f"{label}: {e.stack or e}"))
        return page

    def expected(self, message):
        """Errors the browser logs for answers the page expects: /compiles/latest
        before the first compile, the compile routes without a compile service,
        and the refusals a test asks for."""
        url = (message.location or {}).get("url", "")
        if "/compiles/latest" in url or "/compile" in url:
            return True
        return any(part in url and str(status) in message.text for part, status in self.allowed)

    def go(self, page, rest, paper=None):
        page.goto(f"{self.base}/p/{(paper or self.paper)['name']}{rest}")
        return page

    def shot(self, page, name):
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(self.shots / f"{name}.png"), full_page=True)


# --- helpers ------------------------------------------------------------------------------


def wait_until(fn, timeout=5.0, interval=0.05, what="condition"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what} (last: {last!r})")


def no_page_scroll(page: Page, width: int):
    over = page.evaluate("document.documentElement.scrollWidth")
    assert over <= width, f"the page is {over} px wide at {width}"


def entry_row(page: Page, key: str):
    return page.locator(".rf-entry").filter(has=page.locator(".rf-toggle .rf-key", has_text=re.compile(f"^{re.escape(key)}$")))


def open_row(page: Page, key: str):
    r = entry_row(page, key)
    toggle = r.locator(".rf-toggle")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    return r


def search(page: Page):
    return page.get_by_role("searchbox", name="Find a reference by key, title or author")


def keys_shown(page: Page):
    return page.locator(".rf-toggle .rf-key").all_text_contents()


def line_of(text, needle):
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} is not in the text")


def cm_eval(page, body, arg=None):
    """Run `body` with `view`, the open file's EditorView."""
    return page.evaluate(
        """async ([body, arg]) => {
            const { EditorView } = await import('/static/vendor/editor/editor.js');
            const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
            return new Function('view', 'arg', body)(view, arg);
        }""",
        [body, arg],
    )


def cursor_line(page):
    return cm_eval(page, "return view.state.doc.lineAt(view.state.selection.main.head).number;")


def report_route(env, page, change, counter=None):
    """Answer GET .../references with the server's report, changed by `change`.
    Other methods go on to the server."""
    pattern = re.compile(re.escape(f"{env.base}/api/projects/{env.paper['id']}/references") + r"(\?.*)?$")

    def handle(route):
        if route.request.method != "GET":
            route.fallback()
            return
        if counter is not None:
            counter.append(time.monotonic())
        res = route.fetch()
        data = res.json()
        change(data)
        route.fulfill(response=res, json=data)

    page.route(pattern, handle)
    return pattern


def network_on(data):
    data["network_enabled"] = True
    data["claims_enabled"] = True


# --- tests ----------------------------------------------------------------------------------


@test
def tab_and_counter(env, browser):
    report = env.report()
    statuses = {e["key"]: e["status"] for e in report["entries"]}
    assert statuses == {"roussou2019": "verified", "vaswani2017": "warning", "fake2023": "error",
                        "knuth:1984/tex": "verified", "green2020": "warning", "pending2021": "pending"}, statuses
    assert report["problems"] == 2 and len(report["broken"]) == 1, (report["problems"], report["broken"])
    assert [m["key"] for m in report["missing"]] == ["nothere"], report["missing"]

    ctx = env.context(browser, "alice")
    page = env.go(env.page(ctx, "alice"), "")
    tabs = page.locator(".underline-nav-item")
    expect(tabs.nth(5)).to_contain_text("Reviews")
    expect(tabs.nth(6)).to_contain_text("References")
    expect(tabs.nth(7)).to_contain_text("Settings")
    count = tabs.nth(6).locator(".counter")
    expect(count).to_have_text("2")
    expect(count).to_have_class(re.compile(r"\bcounter-attention\b"))
    tabs.nth(6).click()
    expect(page).to_have_url(re.compile(r"/references$"))
    expect(page.locator(".underline-nav-item[aria-current='page']")).to_contain_text("References")
    head = page.locator(".rf-subhead")
    expect(head.locator("h2")).to_have_text("References 6")
    counts = page.locator(".rf-counts")
    for text in ("1 error", "2 warnings", "1 pending", "2 verified", "1 broken entry"):
        expect(counts).to_contain_text(text)
    expect(page.locator(".rf-run")).to_contain_text(re.compile(r"Last checked 2 hours ago by Alice Andersson, with the claims\."))
    # Lookups are off on this server, so the check buttons are not there.
    expect(page.get_by_text("Reference lookups are off on this server")).to_be_visible()
    expect(page.get_by_role("button", name="Check references")).to_have_count(0)
    expect(page.get_by_role("button", name="Add a reference")).to_have_count(0)
    expect(page.locator(".rf-entry")).to_have_count(6)
    assert keys_shown(page) == ["roussou2019", "vaswani2017", "fake2023", "knuth:1984/tex", "green2020", "pending2021"]
    vaswani = entry_row(page, "vaswani2017")
    expect(vaswani.locator(".rf-status")).to_have_text("Warning")
    expect(vaswani.locator(".rf-title")).to_have_text("Attention Is All We Need")
    expect(vaswani.locator(".rf-sub")).to_have_text("Vaswani, Shazeer, Parmar et al. · 2017 · refs.bib:9")
    expect(vaswani.locator(".rf-count")).to_have_text("1")
    expect(entry_row(page, "knuth:1984/tex").locator(".rf-title")).to_have_text("The TeXbook")
    expect(entry_row(page, "fake2023").locator(".rf-status .Label")).to_have_class(re.compile(r"\blabel-danger\b"))
    expect(entry_row(page, "pending2021").locator(".rf-status")).to_have_text("Pending")
    env.shot(page, "references-1280")
    ctx.close()


@test
def filter_and_search(env, browser):
    ctx = env.context(browser, "alice")
    page = env.go(env.page(ctx, "alice"), "/references")
    expect(page.locator(".rf-entry")).to_have_count(6)
    page.locator(".rf-filter").get_by_role("button", name=re.compile("^Needs a look")).click()
    expect(page).to_have_url(re.compile(r"\?status=problems$"))
    expect(page.locator(".rf-entry")).to_have_count(3)
    assert keys_shown(page) == ["vaswani2017", "fake2023", "green2020"], keys_shown(page)
    search(page).fill("knuth")
    expect(page.get_by_text("No entries match")).to_be_visible()
    expect(page).to_have_url(re.compile(r"status=problems&q=knuth$"))
    page.get_by_role("button", name="Clear the filter").click()
    expect(page.locator(".rf-entry")).to_have_count(6)
    expect(search(page)).to_have_value("")
    # Keys, titles and authors, without case and accents.
    for query, keys in (("roussou", ["roussou2019"]), ("shazeer", ["vaswani2017"]), ("carbon accounting", ["fake2023"]),
                        ("TEXBOOK", ["knuth:1984/tex"]), ("1984/tex", ["knuth:1984/tex"])):
        search(page).fill(query)
        expect(page.locator(".rf-toggle .rf-key")).to_have_text(keys)
    # The query string brings the filter and the search back.
    env.go(page, "/references?status=pending&q=note")
    expect(page.locator(".rf-filter button[aria-pressed='true']")).to_contain_text("Pending")
    expect(search(page)).to_have_value("note")
    expect(page.locator(".rf-entry")).to_have_count(1)
    assert keys_shown(page) == ["pending2021"]
    page.locator(".rf-filter").get_by_role("button", name=re.compile("^Verified")).click()
    expect(page.get_by_text("No entries match")).to_be_visible()
    search(page).fill("")
    expect(page.locator(".rf-entry")).to_have_count(2)
    ctx.close()


@test
def expand_a_row(env, browser):
    ctx = env.context(browser, "alice")
    page = env.go(env.page(ctx, "alice"), "/references")
    r = open_row(page, "vaswani2017")
    detail = r.locator(".rf-detail")
    expect(detail).to_be_visible()
    expect(detail.locator(".rf-finding.is-warning")).to_contain_text("The title is not the record's")
    expect(detail.locator(".rf-finding .Label")).to_have_text("semantic scholar")
    link = detail.locator(".rf-finding a", has_text="Open the record")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener")
    expect(detail.locator(".rf-take code")).to_have_text("Attention Is All You Need")
    expect(detail.get_by_role("button", name="Take this value")).to_be_visible()
    title_row = detail.locator(".rf-compare tbody tr", has=page.locator("th", has_text="Title"))
    expect(title_row).to_have_class(re.compile(r"\bis-differs\b"))
    expect(title_row.locator("td").nth(0)).to_have_text("Attention Is All We Need")
    expect(title_row.locator("td").nth(1)).to_have_text("Attention Is All You Need")
    authors_row = detail.locator(".rf-compare tbody tr", has=page.locator("th", has_text="Authors"))
    expect(authors_row.locator("td").nth(0)).to_have_text("Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit")
    expect(detail.locator(".rf-compare thead")).to_contain_text("The record from semantic scholar")
    expect(detail.locator(".rf-facts")).to_contain_text("Looked up 2 hours ago at semantic scholar.")
    expect(detail.locator(".rf-facts")).to_contain_text("Cited in main.tex:4.")
    expect(detail.get_by_role("link", name="Open in the editor")).to_have_attribute("href", re.compile(r"/edit/refs\.bib#L9$"))
    expect(detail.get_by_role("button", name="Mark as checked")).to_be_visible()

    # The keyboard opens and closes a row.
    toggle = entry_row(page, "fake2023").locator(".rf-toggle")
    toggle.focus()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(entry_row(page, "fake2023").locator(".rf-finding.is-error")).to_contain_text("doi.org does not know the DOI")
    # No record, so nothing to take and no table.
    expect(entry_row(page, "fake2023").locator(".rf-take")).to_have_count(0)
    expect(entry_row(page, "fake2023").locator(".rf-compare")).to_have_count(0)
    page.keyboard.press(" ")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(entry_row(page, "fake2023").locator(".rf-detail")).to_be_hidden()

    # A claim with its verdict, the sentence, the passage and its line.
    r = open_row(page, "green2020")
    claims = r.locator(".rf-claims")
    expect(claims.locator(".rf-claims-head")).to_contain_text("Claims 1")
    claim = claims.locator(".rf-claim")
    expect(claim.locator(".Label")).to_have_text("Not supported")
    expect(claim.locator(".rf-quote").first).to_have_text("Software uses energy, and the meters say so [green2020].")
    expect(claim.locator(".rf-claim-reason")).to_contain_text("The paper measures web servers, not meters.")
    expect(claim.locator(".rf-quote-source")).to_have_text("We measured the energy use of three web servers under load.")
    expect(claim.get_by_role("link", name="main.tex:6")).to_have_attribute("href", re.compile(r"/edit/main\.tex#L6$"))
    # A claim is not one of the findings above it.
    expect(r.locator(".rf-findings")).to_have_count(0)
    env.shot(page, "references-open-1280")
    ctx.close()


@test
def take_this_value(env, browser):
    ctx = env.context(browser, "bob")
    page = env.go(env.page(ctx, "bob"), "/references")
    r = open_row(page, "vaswani2017")
    r.get_by_role("button", name="Take this value").click()
    expect(page.locator(".flash-success")).to_contain_text("Set title of vaswani2017.")
    bib = wait_until(lambda: (t := env.file("bob", "refs.bib")) and "title = {Attention Is All You Need}" in t and t,
                     what="the record's title in refs.bib")
    assert "Attention Is All We Need" not in bib
    # The entry reads differently now, so no run has looked it up yet.
    r = entry_row(page, "vaswani2017")
    expect(r.locator(".rf-status")).to_have_text("Pending")
    expect(r.locator(".rf-title")).to_have_text("Attention Is All You Need")
    expect(r.locator(".rf-toggle")).to_have_attribute("aria-expanded", "true")
    expect(r.locator(".rf-toggle")).to_be_focused()
    expect(page.locator(".rf-counts")).to_contain_text("2 pending")
    ctx.close()


@test
def mark_and_unmark(env, browser):
    key = "knuth:1984/tex"
    ctx = env.context(browser, "bob")
    page = env.go(env.page(ctx, "bob"), "/references")
    r = open_row(page, key)
    r.get_by_role("button", name="Mark as checked").click()
    expect(page.locator(".flash-success")).to_contain_text(f"Marked {key} as checked.")
    r = entry_row(page, key)
    expect(r.locator(".rf-status")).to_have_text("Checked")
    today = datetime.now(UTC).date().isoformat()
    expect(r.locator(".rf-facts")).to_contain_text(f"Marked as checked by Bob Brown on {today}.")
    bib = env.file("bob", "refs.bib")
    assert re.search(r"meertex-checked = \{" + today + r", Bob Brown, [0-9a-f]{8}\}", bib), bib

    r.get_by_role("button", name="Remove the mark").click()
    expect(page.locator(".flash-success")).to_contain_text(f"Removed the mark from {key}.")
    expect(entry_row(page, key).locator(".rf-status")).to_have_text("Verified")
    assert "meertex-checked" not in env.file("bob", "refs.bib")

    # Marked, then changed by someone: Changed, counted as a problem.
    entry_row(page, key).get_by_role("button", name="Mark as checked").click()
    expect(entry_row(page, key).locator(".rf-status")).to_have_text("Checked")
    env.put("alice", "refs.bib", env.file("bob", "refs.bib").replace("Addison-Wesley", "Addison Wesley"))
    page.reload()
    r = open_row(page, key)
    expect(r.locator(".rf-status")).to_have_text("Changed")
    expect(r.locator(".rf-facts")).to_contain_text(f"Bob Brown marked it as checked on {today}. The entry changed since.")
    expect(page.locator(".underline-nav-item", has_text="References").locator(".counter")).to_have_text("3")
    expect(r.get_by_role("button", name="Mark as checked again")).to_be_visible()
    r.get_by_role("button", name="Remove the mark").click()
    expect(entry_row(page, key).locator(".rf-status")).to_have_text("Pending")
    expect(page.locator(".underline-nav-item", has_text="References").locator(".counter")).to_have_text("2")
    ctx.close()


@test
def broken_and_missing(env, browser):
    ctx = env.context(browser, "alice")
    page = env.go(env.page(ctx, "alice"), "/references")
    bib = env.file("alice", "refs.bib")
    broken = page.locator(".rf-section", has=page.locator("h3", has_text="Broken entries"))
    expect(broken.locator("h3")).to_have_text("Broken entries 1")
    line = line_of(bib, "@article{broken")
    expect(broken.get_by_role("link", name=f"refs.bib:{line}")).to_have_attribute("href", re.compile(rf"/edit/refs\.bib#L{line}$"))
    missing = page.locator(".rf-section", has=page.locator("h3", has_text="Missing keys"))
    expect(missing.locator("h3")).to_have_text("Missing keys 1")
    expect(missing.locator(".rf-key")).to_have_text("nothere")
    missing.get_by_role("link", name="main.tex:5").click()
    expect(page).to_have_url(re.compile(r"/edit/main\.tex#L5$"))
    expect(page.locator(".editor-status")).to_have_text("Connected", timeout=10000)
    wait_until(lambda: cursor_line(page) == 5, what="the cursor on line 5")
    ctx.close()


@test
def viewer_reads_only(env, browser):
    ctx = env.context(browser, "vic")
    page = env.go(env.page(ctx, "vic"), "/references")
    expect(page.locator(".rf-entry")).to_have_count(6)
    expect(page.get_by_text("Reference lookups are off on this server")).to_have_count(0)
    r = open_row(page, "roussou2019")
    expect(r.locator(".rf-take code")).to_have_text("1--13")
    expect(r.get_by_role("button", name="Take this value")).to_have_count(0)
    expect(r.get_by_role("button", name="Mark as checked")).to_have_count(0)
    expect(r.get_by_role("link", name="Open in the editor")).to_be_visible()
    ctx.close()


@test
def checks_with_lookups(env, browser):
    """A server with lookups, played by the browser's routes: the buttons, a
    503, the claims' warning and a run's progress until its end."""
    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    reads = []
    finished = {"run": None}  # the run the report names once the run is over

    def change(data):
        network_on(data)
        if finished["run"]:
            data["run"] = finished["run"]

    report_route(env, page, change, counter=reads)
    env.go(page, "/references")
    expect(page.get_by_text("Reference lookups are off on this server")).to_have_count(0)
    check = page.get_by_role("button", name="Check references")
    claims = page.get_by_role("button", name="Check claims")
    expect(check).to_be_visible()
    expect(claims).to_be_visible()
    expect(page.get_by_role("button", name="Add a reference")).to_be_visible()

    # The real server has lookups off and answers 503, shown as a flash.
    env.allowed.append(("/references/check", 503))
    check.click()
    expect(page.locator(".flash-error")).to_contain_text("Reference lookups are off on this server. REFERENCES_NETWORK is false.")
    page.locator(".flash-error .flash-close").click()

    # Claims cost money, and the dialog says so. Cancel sends nothing.
    posts = []
    page.on("request", lambda r: r.method == "POST" and "/references/check" in r.url and posts.append(r.post_data))
    claims.click()
    dialog = page.locator("dialog")
    expect(dialog).to_contain_text("Every check of claims asks Claude, and that costs money.")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    assert posts == [], posts

    # A run from 1 of 4 to its end, asked for every 1.5 seconds.
    run_id = str(uuid.uuid4())
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run = {"id": run_id, "status": "running", "claims": True, "refresh": False, "total": 4, "done": 1, "error": None,
           "started_at": started, "finished_at": None,
           "created_by": {"id": "x", "email": env.people["alice"][0], "name": "Alice Andersson"}}
    finished["run"] = {**run, "status": "done", "done": 4, "finished_at": started}
    polls = []

    def check_route(route):
        if route.request.method == "POST":
            route.fulfill(status=202, json=run)
        else:
            route.fallback()

    def poll_route(route):
        polls.append(time.monotonic())
        if len(polls) < 3:
            route.fulfill(json={**run, "done": len(polls) + 1})
        else:
            route.fulfill(json=finished["run"])

    page.route(re.compile(r".*/references/check$"), check_route)
    page.route(re.compile(rf".*/references/check/{run_id}$"), poll_route)
    before = len(reads)
    claims.click()
    dialog.get_by_role("button", name="Check claims").click()
    assert wait_until(lambda: posts, what="the POST") == ['{"claims":true}'], posts
    progress = page.locator(".rf-progress")
    expect(progress).to_be_visible()
    expect(page.locator(".rf-run")).to_contain_text("Checking the references and their claims")
    expect(page.locator(".rf-progress-text")).to_have_text("1 of 4")
    expect(progress).to_have_attribute("aria-valuenow", "1")
    expect(check).to_be_disabled()
    expect(claims).to_be_disabled()
    env.shot(page, "references-running-1280")
    expect(page.locator(".rf-progress-text")).to_have_text("3 of 4", timeout=4000)
    expect(page.locator(".rf-run")).to_have_text("Last checked just now by Alice Andersson, with the claims.", timeout=4000)
    expect(progress).to_have_count(0)
    expect(page.get_by_role("button", name="Check references")).to_be_enabled()
    # The report is read once more when the run is over, and not while it runs.
    assert len(reads) == before + 1, (before, reads)
    assert len(polls) == 3 and all(1.2 < b - a < 3 for a, b in zip(polls, polls[1:])), polls
    ctx.close()


@test
def add_a_reference(env, browser):
    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    report_route(env, page, network_on)
    env.go(page, "/references")
    page.get_by_role("button", name="Add a reference").click()
    dialog = page.locator("dialog")
    expect(dialog.get_by_role("combobox", name="File")).to_have_value("")
    expect(dialog.get_by_role("combobox", name="File")).to_contain_text("refs.bib (the default)")

    # The real server cannot look anything up and answers 503.
    env.allowed.append(("/references", 503))
    dialog.get_by_label("DOI, arXiv id, ISBN or link").fill("10.1145/3290605.3300857")
    dialog.get_by_role("button", name="Preview").click()
    expect(dialog.locator(".rf-dialog-message")).to_contain_text("Reference lookups are off on this server.")

    # Now a server with lookups: a DOI nobody knows, then a preview and Add.
    added = ("@inproceedings{katifori2020,\n  title = {Cultivating Mobile Experiences Through Interactive Storytelling},\n"
             "  author = {Katifori, Akrivi and Roussou, Maria},\n  year = {2020},\n  doi = {10.1145/3313831.3376000},\n}\n")
    bodies = []

    def add_route(route):
        if route.request.method != "POST":
            route.fallback()
            return
        body = json.loads(route.request.post_data)
        bodies.append(body)
        if body["source"] == "10.1000/none":
            route.fulfill(status=404, json={"detail": "doi.org does not know the DOI 10.1000/none"})
        elif body.get("dry_run"):
            route.fulfill(status=200, json={"key": "katifori2020", "path": "refs.bib", "line": 70, "bibtex": added})
        else:
            env.put("alice", "refs.bib", env.file("alice", "refs.bib").rstrip("\n") + "\n\n" + added)
            route.fulfill(status=201, json={"key": "katifori2020", "path": "refs.bib", "line": 70, "bibtex": added})

    page.route(re.compile(re.escape(f"{env.base}/api/projects/{env.paper['id']}/references") + "$"), add_route)
    env.allowed.append(("/references", 404))
    dialog.get_by_label("DOI, arXiv id, ISBN or link").fill("10.1000/none")
    dialog.get_by_role("button", name="Preview").click()
    expect(dialog.locator(".rf-dialog-message")).to_have_text("doi.org does not know the DOI 10.1000/none")
    dialog.get_by_label("DOI, arXiv id, ISBN or link").fill("10.1145/3313831.3376000")
    dialog.get_by_role("button", name="Preview").click()
    expect(dialog.locator(".rf-dialog-message")).to_be_empty()
    expect(dialog.locator("pre.rf-bibtex")).to_have_text(added)
    expect(dialog.locator(".rf-preview")).to_contain_text("It goes into refs.bib as katifori2020, at line 70.")
    env.shot(page, "references-add-1280")
    # Changing an input asks for a new preview.
    dialog.get_by_label("Key").fill("x")
    expect(dialog.get_by_role("button", name="Preview")).to_be_visible()
    expect(dialog.locator(".rf-preview")).to_be_hidden()
    dialog.get_by_label("Key").fill("")
    dialog.get_by_role("button", name="Preview").click()
    dialog.get_by_role("button", name="Add", exact=True).click()
    expect(dialog).to_have_count(0)
    expect(page.locator(".flash-success")).to_contain_text("Added katifori2020 to refs.bib.")
    r = entry_row(page, "katifori2020")
    expect(r.locator(".rf-toggle")).to_have_attribute("aria-expanded", "true")
    expect(r.locator(".rf-status")).to_have_text("Pending")
    assert [b.get("dry_run", False) for b in bodies] == [True, True, True, False], bodies
    assert bodies[-1] == {"source": "10.1145/3313831.3376000", "key": None, "path": None}, bodies[-1]
    ctx.close()


@test
def editor_shows_reference_problems(env, browser):
    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    asked = []
    page.on("request", lambda r: r.url.endswith("/references/problems") and asked.append(r.url))
    env.go(page, "/edit/main.tex")
    expect(page.locator(".editor-status")).to_have_text("Connected", timeout=10000)
    head = page.locator(".pdf-problems-head")
    expect(head).to_be_visible()
    expect(head).to_contain_text("2 errors")
    # It stays closed for the reference check's errors alone.
    expect(head).to_have_attribute("aria-expanded", "false")
    head.click()
    items = page.locator(".pdf-problem")
    fake = items.filter(has_text="fake2023: doi.org does not know the DOI")
    expect(fake).to_have_count(1)
    expect(fake.locator(".Label")).to_have_text("References")
    expect(fake).to_have_class(re.compile(r"\bis-error\b"))
    expect(items.filter(has_text="There is no entry nothere in the .bib files.")).to_have_count(1)
    expect(items.filter(has_text="green2020: The source does not support this sentence.")).to_have_count(1)
    assert len(asked) == 1, asked

    # On their lines in the text, said to come from the reference check.
    warning = page.locator(".cm-problemLine.is-warning")
    expect(warning).to_have_count(2)
    expect(warning.first).to_contain_text("\\citep{fake2023, nothere}")
    page.locator(".cm-lint-marker-warning").first.hover()
    tooltip = page.locator(".cm-tooltip-lint")
    expect(tooltip).to_contain_text("There is no entry nothere in the .bib files.")
    expect(tooltip).to_contain_text("References")
    expect(tooltip).not_to_contain_text("LaTeX")
    page.mouse.move(5, 5)

    bib = env.file("alice", "refs.bib")
    line = line_of(bib, "doi = {10.1145/9999999.9999999}")
    fake.locator(".pdf-problem-where").click()
    expect(page).to_have_url(re.compile(r"/edit/refs\.bib$"))
    error = page.locator(".cm-problemLine.is-error")
    expect(error.filter(has_text="doi = {10.1145/9999999.9999999}")).to_have_count(1)
    wait_until(lambda: cursor_line(page) == line, what=f"the cursor on line {line}")
    env.shot(page, "editor-reference-problems-1280")
    ctx.close()


@test
def editor_link_to_an_entry(env, browser):
    ctx = env.context(browser, "alice")
    page = env.go(env.page(ctx, "alice"), "/references")
    line = line_of(env.file("alice", "refs.bib"), "@article{fake2023,")
    r = open_row(page, "fake2023")
    r.get_by_role("link", name="Open in the editor").click()
    expect(page).to_have_url(re.compile(rf"/edit/refs\.bib#L{line}$"))
    expect(page.locator(".editor-status")).to_have_text("Connected", timeout=10000)
    wait_until(lambda: cursor_line(page) == line, what=f"the cursor on line {line}")
    ctx.close()


@test
def phone_width(env, browser):
    ctx = env.context(browser, "bob", width=390, height=844)
    page = env.page(ctx, "bob")
    report_route(env, page, network_on)
    env.go(page, "/references")
    expect(page.locator(".rf-entry").first).to_be_visible()
    no_page_scroll(page, 390)
    open_row(page, "fake2023")
    r = open_row(page, "green2020")
    expect(r.locator(".rf-claim")).to_be_visible()
    open_row(page, "roussou2019")
    no_page_scroll(page, 390)
    env.shot(page, "references-390")
    page.get_by_role("button", name="Add a reference").click()
    expect(page.locator("dialog")).to_be_visible()
    no_page_scroll(page, 390)
    env.shot(page, "references-add-390")
    ctx.close()


@test
def empty_states(env, browser):
    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    bare = env.create_paper("alice", f"No bib {env.run}")
    with env.api("alice") as c:
        c.delete(env.url("/files/refs.bib", bare)).raise_for_status()
    env.go(page, "/references", bare)
    expect(page.get_by_text("No .bib files")).to_be_visible()
    expect(page.locator(".filter-bar")).to_be_hidden()
    expect(page.locator(".underline-nav-item", has_text="References").locator(".counter")).to_be_hidden()

    empty = env.create_paper("alice", f"Empty bib {env.run}")
    env.put("alice", "refs.bib", "% Nothing here yet.\n", empty)
    env.go(page, "/references", empty)
    expect(page.get_by_text("No entries yet")).to_be_visible()
    expect(page.get_by_text("refs.bib has no entries.", exact=False)).to_be_visible()

    fresh = env.create_paper("alice", f"Fresh bib {env.run}")
    env.put("alice", "refs.bib", "@misc{one,\n  title = {One Entry Only},\n  year = {2020},\n}\n", fresh)
    env.go(page, "/references", fresh)
    expect(page.locator(".rf-entry")).to_have_count(1)
    expect(page.locator(".rf-run")).to_have_text("Not checked yet.")
    expect(page.get_by_text("Nothing is checked yet.")).to_be_visible()
    env.shot(page, "references-unchecked-1280")
    ctx.close()


# --- main -----------------------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8067)
    parser.add_argument("--db", default=os.environ.get("MEERTEX_REFERENCES_UI_DB", DEFAULT_DB))
    parser.add_argument("--shots", help="save screenshots into this folder")
    parser.add_argument("--tmp", help="the folder for the server's data and logs (default: the system's)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder with the server's log")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("only", nargs="*", help="run only the tests with these names (the ones before them run too)")
    args = parser.parse_args()

    env = Env(args)
    failed = 0
    try:
        env.start()
        env.setup()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=not args.headed)
            last = max((i for i, fn in enumerate(TESTS) if fn.__name__ in args.only), default=len(TESTS) - 1)
            for fn in TESTS[: last + 1]:
                t0 = time.monotonic()
                try:
                    detail = fn(env, browser)
                    print(f"ok    {fn.__name__} ({time.monotonic() - t0:.1f} s){f': {detail}' if detail else ''}", flush=True)
                except Exception:  # noqa: BLE001 (a failed test is reported, the run goes on)
                    failed += 1
                    print(f"FAIL  {fn.__name__}", flush=True)
                    traceback.print_exc()
            browser.close()
        if env.errors:
            print("Errors in the browsers' consoles:")
            for e in env.errors:
                print(f"  {e}")
            failed += 1
    finally:
        env.stop()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
