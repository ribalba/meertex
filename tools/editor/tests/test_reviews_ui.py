"""End-to-end tests of the review pages and the comments in the editor.

The Reviews tab (app/static/js/pages/reviews.js), a review's Conversation,
Files and Changes views (review.js, review-files.js), the thread cards they
share (threads.js) and the comments in the browser editor (editor-comments.js).
Google Chrome through Playwright against a real server and a real database.
tools/editor/README.md says how to run it. The tests run in order and build on
each other: a reviewer starts a review, an editor answers it, the reviewer
accepts and reopens. A run prints one line per test and exits 1 when one failed.
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
import traceback
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

REPO = Path(__file__).resolve().parents[3]
PYTHON = REPO / ".venv" / "bin" / "python"
DEFAULT_DB = "postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_reviews_ui"

PAPER = r"""\documentclass{article}
\begin{document}
\section{Introduction}
Grüße aus Köln, sagte die naïve Forscherin im Café.

This paragraph explains the method in far too many words and should be a lot shorter than it is.

The results are good. They are very good indeed.

\section{Conclusion}
We conclude.
\end{document}
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
        self.tmp = Path(tempfile.mkdtemp(prefix="meertex-reviews-", dir=args.tmp))
        self.server = None
        self.run = secrets.token_hex(3)
        self.shots = Path(args.shots) if args.shots else None
        self.errors = []
        self.compile_service = None
        self.compiling = bool(args.compile or args.compile_url)
        # What the tests hand on to each other.
        self.review = None
        self.threads = {}

    # --- server ----------------------------------------------------------------------

    def start_compile_service(self):
        """compile/service.py with this machine's TeX Live, as `make compile-dev` runs it."""
        if not shutil.which("latexmk"):
            raise RuntimeError("--compile needs latexmk, synctex and latexdiff on the PATH")
        self.args.compile_token = secrets.token_hex(16)
        self.args.compile_url = f"http://127.0.0.1:{self.args.compile_port}"
        env = {**os.environ, "COMPILE_TOKEN": self.args.compile_token, "COMPILE_PORT": str(self.args.compile_port),
               "COMPILE_JOBS_DIR": str(self.tmp / "jobs")}
        with open(self.tmp / "compile.log", "a") as log:
            self.compile_service = subprocess.Popen([sys.executable, "-I", str(REPO / "compile" / "service.py")],
                                                    cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if httpx.get(f"{self.args.compile_url}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"The compile service did not start, see {self.tmp / 'compile.log'}")

    def start(self):
        if self.args.compile:
            self.start_compile_service()
        env = {
            **os.environ,
            "DATABASE_URL": self.args.db,
            "DATA_DIR": str(self.tmp / "data"),
            "BASE_URL": self.base,
            "SMTP_HOST": "",
            "COMPILE_URL": self.args.compile_url or "",
            "COMPILE_TOKEN": self.args.compile_token or "",
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
        for proc in (self.server, self.compile_service):
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGINT if proc is self.server else signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.args.keep:
            print(f"Kept {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)

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
        env = {**os.environ, "DATABASE_URL": self.args.db, "DATA_DIR": str(self.tmp / "data")}
        out = subprocess.run([str(PYTHON), "-c", code, *[f"{e}|{n}" for e, n in people]],
                             cwd=REPO, env=env, check=True, capture_output=True, text=True).stdout
        return {line.split()[0]: tuple(line.split()[1:]) for line in out.strip().splitlines()}

    def setup(self):
        r = self.run
        self.people = {
            "alice": (f"alice-{r}@example.com", "Alice Andersson"),
            "bob": (f"bob-{r}@example.com", "Bob Brown"),
            "rita": (f"rita-{r}@example.com", "Rita Reviewer"),
            "vic": (f"vic-{r}@example.com", "Vic Viewer"),
        }
        made = self.make_users(self.people.values())
        self.session = {k: made[e][0] for k, (e, _) in self.people.items()}
        self.token = {k: made[e][1] for k, (e, _) in self.people.items()}
        self.paper = self.create_paper("alice", f"Review test {r}", members={"bob": "editor", "rita": "reviewer", "vic": "viewer"})
        self.put("alice", "main.tex", PAPER)
        with self.api("alice") as c:
            c.delete(f"/api/projects/{self.paper['id']}/files/refs.bib").raise_for_status()
            v = c.post(f"/api/projects/{self.paper['id']}/versions", json={"kind": "submission"})
            v.raise_for_status()
            self.version = v.json()

    def api(self, who):
        return httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {self.token[who]}"}, timeout=60)

    def url(self, rest=""):
        return f"/api/projects/{self.paper['id']}{rest}"

    def create_paper(self, owner, title, members=None):
        with self.api(owner) as c:
            p = c.post("/api/projects", json={"title": title, "conference": "TEST", "year": 2027, "starter": "basic"})
            p.raise_for_status()
            project = p.json()
            for who, role in (members or {}).items():
                c.post(f"/api/projects/{project['id']}/members", json={"email": self.people[who][0], "role": role}).raise_for_status()
        return project

    def file(self, who, path):
        with self.api(who) as c:
            r = c.get(self.url(f"/files/{path}"))
        return r.text if r.status_code == 200 else None

    def put(self, who, path, body):
        with self.api(who) as c:
            c.put(self.url(f"/files/{path}"), content=body.encode()).raise_for_status()

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
        """Errors the browser logs for answers the page expects: the 404 of a
        review somebody else may not see, of /compiles/latest before the first
        compile, and the 503 of the compile routes without a compile service."""
        url = (message.location or {}).get("url", "")
        if "404" in message.text and ("/reviews/" in url or "/compiles/latest" in url or "/synctex/" in url):
            return True
        return not self.compiling and ("/compile" in url or "/pdfdiff" in url)

    def go(self, page, rest):
        page.goto(f"{self.base}/p/{self.paper['name']}{rest}")
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


def select_in_line(page: Page, line: int, needle: str):
    """Select `needle` in a line of the review's Files view, as a mouse would."""
    page.evaluate(
        """([line, needle]) => {
            const td = document.querySelector(`#RL${line} td.lc`);
            const walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
            let node = walker.nextNode();
            while (node && !node.data.includes(needle)) node = walker.nextNode();
            if (!node) throw new Error(`${needle} is not in line ${line}`);
            const i = node.data.indexOf(needle);
            const r = document.createRange();
            r.setStart(node, i);
            r.setEnd(node, i + needle.length);
            const s = getSelection();
            s.removeAllRanges();
            s.addRange(r);
            td.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""",
        [line, needle],
    )


def card(page: Page, thread_id: str):
    return page.locator(f"#thread-{thread_id}")


def no_page_scroll(page: Page, width: int):
    over = page.evaluate("document.documentElement.scrollWidth")
    assert over <= width, f"the page is {over} px wide at {width}"


# --- tests: starting and writing a review ------------------------------------------------------


@test
def start_review_on_a_version(env, browser):
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), "/reviews")
    expect(page.locator(".underline-nav-item[aria-current='page']")).to_contain_text("Reviews")
    expect(page.get_by_text("No reviews yet")).to_be_visible()
    page.locator(".subhead").get_by_role("button", name="Start a review").click()
    dialog = page.locator("dialog")
    dialog.get_by_text(env.version["label"], exact=True).click()
    env.shot(page, "start-1280")
    dialog.get_by_role("button", name="Start review").click()
    expect(page).to_have_url(re.compile(r"/reviews/[0-9a-f-]{36}/files$"))
    env.review = page.url.split("/reviews/")[1].split("/")[0]
    expect(page.locator(".rv-head")).to_contain_text("Pending, only you see it")
    expect(page.locator(".rv-head")).to_contain_text(env.version["tag"])
    expect(page.locator("#RL4 td.lc")).to_contain_text("Grüße aus Köln")
    with env.api("rita") as c:
        review = c.get(env.url(f"/reviews/{env.review}")).json()
    assert review["commit"] == env.version["commit"], review
    ctx.close()
    return f"review {env.review[:8]} on {env.version['tag']}"


@test
def comment_on_a_selection_with_umlauts(env, browser):
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}/files")
    expect(page.locator("#RL4")).to_be_visible()
    select_in_line(page, 4, "Grüße aus Köln")
    float_btn = page.locator(".rv-float")
    expect(float_btn).to_be_visible()
    float_btn.click()
    compose = page.locator(".rv-compose")
    expect(compose).to_contain_text("Comment on line 4")
    expect(compose.locator(".rv-compose-quote")).to_have_text("Grüße aus Köln")
    compose.get_by_label("Comment").fill("Bitte prüfen: die Größe der Grüße passt nicht. 🎉")
    env.shot(page, "compose-1280")
    compose.get_by_role("button", name="Add review comment").click()
    expect(compose).to_have_count(0)
    with env.api("rita") as c:
        threads = c.get(env.url(f"/reviews/{env.review}")).json()["threads"]
    assert len(threads) == 1, threads
    t = threads[0]
    assert t["quote"] == "Grüße aus Köln", t["quote"]
    assert t["base"]["line_start"] == 4 and t["pending"], t
    assert t["comments"][0]["body"] == "Bitte prüfen: die Größe der Grüße passt nicht. 🎉"
    env.threads["greeting"] = t["id"]
    thread = card(page, t["id"])
    expect(thread).to_contain_text("Pending")
    expect(thread).to_contain_text("Bitte prüfen")
    expect(page.locator("#RL4 mark.rv-mark")).to_have_text("Grüße aus Köln")
    ctx.close()


@test
def suggestion_and_line_comments(env, browser):
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}/files")
    expect(page.locator("#RL4 mark.rv-mark")).to_be_visible()
    # A suggestion on a selection.
    select_in_line(page, 4, "naïve Forscherin")
    page.locator(".rv-float").click()
    compose = page.locator(".rv-compose")
    compose.get_by_role("button", name="Suggest a change").click()
    suggestion = compose.get_by_label("Suggested text")
    expect(suggestion).to_have_value("naïve Forscherin")
    suggestion.fill("naive Forscherin")
    compose.get_by_label("Comment").fill("Simpler spelling, please.")
    compose.get_by_role("button", name="Add review comment").click()
    expect(compose).to_have_count(0)
    # A comment on a whole line, from its line number.
    page.locator("#RL6 td.rv-ln").click()
    expect(compose).to_contain_text("Comment on line 6")
    compose.get_by_label("Comment").fill("This paragraph is too long.")
    compose.get_by_role("button", name="Add review comment").click()
    expect(compose).to_have_count(0)
    # A range with a shift-click, then cancelled.
    page.locator("#RL8 td.rv-ln").click()
    page.locator("#RL10 td.rv-ln").click(modifiers=["Shift"])
    expect(compose).to_contain_text("Comment on lines 8 to 10")
    expect(page.locator("tr.rv-line.is-chosen")).to_have_count(3)
    compose.get_by_role("button", name="Cancel").click()
    expect(compose).to_have_count(0)

    with env.api("rita") as c:
        threads = c.get(env.url(f"/reviews/{env.review}")).json()["threads"]
    by_quote = {t["quote"]: t for t in threads}
    sugg = by_quote["naïve Forscherin"]
    assert sugg["comments"][0]["suggestion"] == "naive Forscherin", sugg
    line = next(t for t in threads if t["base"]["line_start"] == 6)
    assert line["quote"].startswith("This paragraph explains") and line["quote"].endswith("than it is."), line["quote"]
    env.threads["suggestion"] = sugg["id"]
    env.threads["paragraph"] = line["id"]
    expect(card(page, sugg["id"]).locator(".rv-suggestion")).to_contain_text("Suggested change")
    expect(card(page, sugg["id"]).locator(".rv-suggestion del")).to_have_text("naïve")
    expect(card(page, sugg["id"]).locator(".rv-suggestion ins")).to_have_text("naive")
    expect(page.locator(".rv-tab[aria-current='page']")).to_contain_text("Files")
    expect(page.locator(".rv-tab").first.locator(".counter")).to_have_text("3")
    env.shot(page, "files-pending-1280")
    ctx.close()


@test
def pending_threads_are_invisible_to_others(env, browser):
    with env.api("bob") as c:
        assert c.get(env.url("/reviews")).json() == []
        assert c.get(env.url("/threads"), params={"state": "all"}).json() == []
        assert c.get(env.url(f"/reviews/{env.review}")).status_code == 404
    ctx = env.context(browser, "bob")
    page = env.go(env.page(ctx, "bob"), "/reviews")
    expect(page.get_by_text("No reviews yet")).to_be_visible()
    expect(page.get_by_text("No open conversations")).to_be_visible()
    expect(page.locator(".underline-nav-item .rv-open-count")).to_have_text("0")
    env.go(page, f"/reviews/{env.review}")
    expect(page.locator(".blankslate")).to_contain_text("This review is not here")
    ctx.close()


@test
def submit_with_a_verdict(env, browser):
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}")
    expect(page.locator(".rv-thread")).to_have_count(3)
    expect(page.locator(".rv-view .flash")).to_contain_text("This review is pending.")
    page.locator(".rv-head").get_by_role("button", name="Finish your review").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Summary").fill("Schöne Arbeit. A few things to fix before the camera-ready.")
    dialog.get_by_text("Request changes", exact=True).click()
    env.shot(page, "finish-1280")
    dialog.get_by_role("button", name="Submit review").click()
    expect(page.locator(".rv-head")).to_contain_text("Changes requested")
    expect(page.locator(".rv-summary")).to_contain_text("Schöne Arbeit.")
    expect(page.locator(".rv-head").get_by_role("button", name="Finish your review")).to_have_count(0)
    with env.api("rita") as c:
        review = c.get(env.url(f"/reviews/{env.review}")).json()
    assert review["state"] == "submitted" and review["verdict"] == "changes_requested", review
    assert not any(t["pending"] for t in review["threads"])
    ctx.close()


# --- tests: the authors answer ------------------------------------------------------------------


@test
def editor_replies_applies_and_resolves(env, browser):
    ctx = env.context(browser, "bob")
    page = env.go(env.page(ctx, "bob"), "/reviews")
    expect(page.locator(".underline-nav-item .rv-open-count")).to_have_text("3")
    expect(page.locator(".rv-review-row")).to_contain_text("Review by Rita Reviewer")
    expect(page.locator(".rv-review-row")).to_contain_text("Changes requested")
    cards = page.locator(".rv-thread-list .rv-thread")
    expect(cards).to_have_count(3)
    # In file order, with the text as it reads now and the lines around it.
    greeting = card(page, env.threads["greeting"])
    expect(greeting.locator(".rv-snippet mark.rv-hl")).to_have_text("Grüße aus Köln")
    expect(greeting.locator(".rv-snippet tr")).to_have_count(5)
    env.shot(page, "reviews-1280")

    # A reply.
    paragraph = card(page, env.threads["paragraph"])
    paragraph.get_by_role("button", name="Reply…").click()
    paragraph.get_by_label("Reply").fill("Shortened it in the next snapshot.")
    paragraph.get_by_role("button", name="Reply", exact=True).click()
    expect(paragraph.locator(".rv-comment")).to_have_count(2)
    expect(paragraph.locator(".rv-comment").nth(1)).to_contain_text("Bob Brown")

    # Applying the suggestion writes it into the live document and resolves.
    sugg = card(page, env.threads["suggestion"])
    sugg.get_by_role("button", name="Apply suggestion").click()
    expect(sugg.locator(".rv-labels")).to_contain_text("Resolved")
    expect(sugg.locator(".rv-event")).to_contain_text("applied the suggestion")
    wait_until(lambda: "die naive Forscherin" in (env.file("bob", "main.tex") or ""), what="the suggestion in the live file")

    # A change to the text, then resolving with a note.
    env.put("bob", "main.tex", env.file("bob", "main.tex").replace("Grüße aus Köln", "Grüße aus dem schönen Köln"))
    page.reload()
    greeting = card(page, env.threads["greeting"])
    expect(greeting.locator(".rv-labels")).to_contain_text("Changed")
    expect(greeting.locator(".rv-snippet mark.rv-hl")).to_have_text("Grüße aus dem schönen Köln")
    greeting.get_by_role("button", name="Resolve conversation").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Note").fill("Made the greeting more specific, ä ö ü.")
    dialog.get_by_role("button", name="Resolve conversation").click()
    expect(greeting.locator(".rv-labels")).to_contain_text("Resolved")
    expect(greeting.locator(".rv-resolution ins")).to_contain_text("dem schönen")
    expect(greeting.locator(".rv-event-note")).to_contain_text("Made the greeting more specific, ä ö ü.")
    # Bob is no creator and no owner, so he cannot accept.
    expect(greeting.get_by_role("button", name="Accept")).to_have_count(0)
    expect(page.locator(".underline-nav-item .rv-open-count")).to_have_text("1")
    page.locator(".segmented").get_by_role("button", name="All").click()
    expect(page.locator(".rv-thread-list .rv-thread")).to_have_count(3)
    env.shot(page, "reviews-all-1280")
    ctx.close()


@test
def reviewer_accepts_one_and_reopens_the_other(env, browser):
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}")
    greeting = card(page, env.threads["greeting"])
    expect(greeting.locator(".rv-resolution")).to_contain_text("How it was changed")
    expect(greeting.locator(".rv-resolution ins")).to_contain_text("dem schönen")
    expect(greeting).to_contain_text("Check the change below")
    env.shot(page, "conversation-1280")
    greeting.get_by_role("button", name="Reopen").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Note").fill("Still not quite right.")
    dialog.get_by_role("button", name="Reopen").click()
    expect(greeting.locator(".rv-labels")).to_contain_text("Open")
    expect(greeting.locator(".rv-event").last).to_contain_text("reopened this conversation")
    expect(greeting.locator(".rv-resolution")).to_have_count(0)

    sugg = card(page, env.threads["suggestion"])
    expect(sugg.locator(".rv-resolution ins")).to_have_text("naive")
    sugg.get_by_role("button", name="Accept").click()
    expect(sugg).to_have_class(re.compile("is-collapsed"))
    expect(sugg.locator(".rv-labels")).to_contain_text("Accepted")
    expect(page.locator(".rv-counts")).to_contain_text("2 open")
    expect(page.locator(".rv-counts")).to_contain_text("1 accepted")

    with env.api("rita") as c:
        states = {t["id"]: t["state"] for t in c.get(env.url(f"/reviews/{env.review}")).json()["threads"]}
    assert states[env.threads["greeting"]] == "open" and states[env.threads["suggestion"]] == "accepted", states
    # Editing her own comment.
    first = greeting.locator(".rv-comment").first
    first.get_by_label("Comment actions").click()
    page.get_by_role("menuitem", name="Edit").click()
    first.get_by_label("Edit the comment").fill("Bitte prüfen, die Größe passt nicht.")
    first.get_by_role("button", name="Save").click()
    expect(first).to_contain_text("edited")
    expect(first.locator(".markdown")).to_have_text("Bitte prüfen, die Größe passt nicht.")
    ctx.close()


@test
def viewer_cannot_comment(env, browser):
    ctx = env.context(browser, "vic")
    page = env.go(env.page(ctx, "vic"), "/reviews")
    expect(page.locator(".rv-review-row")).to_have_count(1)
    expect(page.get_by_role("button", name="Start a review")).to_have_count(0)
    expect(page.locator(".rv-thread").first).to_be_visible()
    expect(page.get_by_role("button", name="Reply…")).to_have_count(0)
    expect(page.get_by_role("button", name="Resolve conversation")).to_have_count(0)
    env.go(page, f"/reviews/{env.review}/files")
    expect(page.get_by_text("You can read this review.")).to_be_visible()
    expect(page.locator("td.rv-ln")).to_have_count(0)
    select_in_line(page, 11, "We conclude")
    page.wait_for_timeout(400)
    expect(page.locator(".rv-float")).to_be_hidden()
    ctx.close()


@test
def outdated_thread_after_the_text_is_gone(env, browser):
    text = env.file("alice", "main.tex")
    start = text.index("We conclude.")
    with env.api("alice") as c:
        r = c.post(env.url("/threads"), json={"path": "main.tex", "start": len(text[:start]), "end": len(text[:start]) + len("We conclude."),
                                              "body": "Say what we conclude."})
        r.raise_for_status()
        single = r.json()
    env.put("alice", "main.tex", text.replace("We conclude.\n", ""))
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews#thread-{single['id']}")
    thread = card(page, single["id"])
    expect(thread).to_have_class(re.compile("is-target"))
    expect(thread.locator(".rv-labels")).to_contain_text("Outdated")
    expect(thread).to_contain_text("Single comment")
    expect(thread.locator(".rv-snippet.is-outdated")).to_contain_text("We conclude.")
    expect(thread.get_by_role("button", name="Accept")).to_have_count(0)
    env.shot(page, "outdated-1280")
    ctx.close()
    env.threads["single"] = single["id"]


@test
def back_and_forward_between_views(env, browser):
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}")
    tab = page.locator(".rv-tab[aria-current='page']")
    expect(tab).to_contain_text("Conversation")
    page.locator(".rv-tab", has_text="Files").click()
    expect(page).to_have_url(re.compile(r"/files"))
    expect(page.locator("#RL4")).to_be_visible()
    page.locator(".rv-tab", has_text="Changes since this review").click()
    expect(page).to_have_url(re.compile(r"/changes$"))
    expect(page.locator(".diff-file ins").first).to_be_visible()
    expect(page.locator(".diff-file")).to_contain_text("dem schönen")
    env.shot(page, "changes-1280")
    page.go_back()
    expect(tab).to_contain_text("Files")
    expect(page.locator("#RL4")).to_be_visible()
    page.go_back()
    expect(tab).to_contain_text("Conversation")
    expect(page.locator(".rv-thread")).to_have_count(3)
    page.go_forward()
    expect(tab).to_contain_text("Files")
    page.go_forward()
    expect(tab).to_contain_text("Changes")
    ctx.close()


@test
def files_fill_the_window(env, browser):
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}/files")
    expect(page.locator("#RL1")).to_be_visible()
    # LaTeX in the editor's colours, also under a thread's mark.
    expect(page.locator("#RL1 .tex-command")).to_have_text("\\documentclass")
    expect(page.locator("#RL2 .tex-atom")).to_have_text("document")
    expect(page.locator("#RL3 .tex-command")).to_have_text("\\section")
    expect(page.locator("#RL4 mark.rv-mark").first).to_be_visible()
    # The source and the PDF fill the window below the header, as wide as it is.
    page.wait_for_timeout(300)
    box = page.locator(".rv-files-split").bounding_box()
    assert abs(box["y"] + box["height"] - 900) <= 1 and box["width"] >= 1260, box
    # The source scrolls on its own and the page stays.
    scroll = page.locator(".rv-code-scroll")
    scroll.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    assert scroll.evaluate("el => el.scrollTop") > 0
    assert page.evaluate("window.scrollY") == 0
    scroll.evaluate("el => { el.scrollTop = 0; }")
    env.shot(page, "files-1280")
    ctx.close()


@test
def phone_width(env, browser):
    ctx = env.context(browser, "bob", width=390, height=844)
    page = env.page(ctx, "bob")
    for name, rest, ready in (
        ("reviews", "/reviews", ".rv-thread"),
        ("conversation", f"/reviews/{env.review}", ".rv-thread"),
        ("files", f"/reviews/{env.review}/files", "#RL4"),
        ("changes", f"/reviews/{env.review}/changes", ".diff-file"),
    ):
        env.go(page, rest)
        expect(page.locator(ready).first).to_be_visible()
        page.wait_for_timeout(300)
        no_page_scroll(page, 390)
        env.shot(page, f"{name}-390")
    ctx.close()


# --- tests: the PDF (step 2) ------------------------------------------------------------------------


@test
def pdf_next_to_the_source(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}/files")
    pdf = page.locator(".rv-pdf-col .pdfview-page")
    expect(pdf.first).to_be_visible(timeout=120000)
    # A thread marks its lines in the PDF.
    card(page, env.threads["paragraph"]).locator(".rv-thread-head").click()
    mark = page.locator(".pdfview-mark").first
    expect(mark).to_be_visible(timeout=10000)
    box = mark.bounding_box()
    env.shot(page, "files-pdf-1280")
    # A double-click in the PDF opens the comment box at that source line.
    page.mouse.dblclick(box["x"] + 20, box["y"] + box["height"] / 2)
    compose = page.locator(".rv-compose")
    expect(compose.locator(".rv-compose-head")).to_have_text(re.compile(r"^Comment on line (5|6|7)$"), timeout=10000)
    compose.get_by_role("button", name="Cancel").click()
    # The toggle hides it and is remembered.
    page.locator(".rv-pdf-toggle").click()
    expect(page.locator(".rv-pdf-col")).to_be_hidden()
    page.reload()
    expect(page.locator("#RL4")).to_be_visible()
    expect(page.locator(".rv-pdf-col")).to_be_hidden()
    page.locator(".rv-pdf-toggle").click()
    expect(pdf.first).to_be_visible(timeout=60000)
    ctx.close()


def text_layer_box(page, needle, chars=None):
    """The screen box of `needle` in the text layer of the review's PDF, or of
    its characters `chars` (a start and an end index)."""
    return page.evaluate(
        """([needle, chars]) => {
            const layer = document.querySelector('.rv-pdf-col .textLayer');
            const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
            for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                const i = node.data.indexOf(needle);
                if (i < 0) continue;
                const [a, b] = chars || [0, needle.length];
                const r = document.createRange();
                r.setStart(node, i + a);
                r.setEnd(node, i + b);
                const { left, top, right, bottom } = r.getBoundingClientRect();
                return { left, top, right, bottom };
            }
            throw new Error(`${needle} is not in the text layer: ${layer.textContent}`);
        }""",
        [needle, chars],
    )


def text_layer_point(page, needle, end=False):
    """The screen point in the first (with end, the last) character of `needle`."""
    k = len(needle) - 1 if end else 0
    box = text_layer_box(page, needle, [k, k + 1])
    frac = 0.75 if end else 0.25
    return {"x": box["left"] + (box["right"] - box["left"]) * frac, "y": (box["top"] + box["bottom"]) / 2}


def drag_in_pdf(page, start, end):
    a = text_layer_point(page, start)
    b = text_layer_point(page, end, end=True)
    page.mouse.move(a["x"], a["y"])
    page.mouse.down()
    page.mouse.move(b["x"], b["y"], steps=8)
    page.mouse.up()


def picked(page):
    return page.evaluate("[...document.querySelectorAll('.rv-code-table .rv-pick')].map((el) => el.textContent).join('')")


@test
def pdf_selection_marks_the_source(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    ctx = env.context(browser, "rita")
    page = env.go(env.page(ctx, "rita"), f"/reviews/{env.review}/files")
    expect(page.locator(".rv-pdf-col .textLayer span").first).to_be_attached(timeout=120000)
    # Across the PDF's line break, which the source does not have.
    drag_in_pdf(page, "method", "shorter")
    wait_until(lambda: picked(page) == "method in far too many words and should be a lot shorter", timeout=10, what="the passage marked")
    assert page.locator(".rv-pick").first.evaluate("el => el.closest('tr').id") == "RL6"
    float_btn = page.locator(".rv-pdf-float")
    expect(float_btn).to_be_visible()
    env.shot(page, "files-pdf-selection-1280")
    float_btn.click()
    compose = page.locator(".rv-compose")
    expect(compose.locator(".rv-compose-head")).to_have_text("Comment on line 6")
    expect(compose.locator(".rv-compose-quote")).to_have_text("method in far too many words and should be a lot shorter")
    expect(page.locator(".rv-pick")).to_have_count(0)
    compose.get_by_role("button", name="Cancel").click()
    # Zoomed in, the selectable text still lies on the drawn one.
    pdf_col = page.locator(".rv-pdf-col")
    pdf_col.get_by_role("button", name="Zoom in").click()
    pdf_col.get_by_role("button", name="Zoom in").click()
    page.wait_for_timeout(500)
    url = pdf_col.locator("a[download]").get_attribute("href")
    drawn = pdf_word_point(env, None, page, "Forscherin", url=url, host=".rv-pdf-col")
    box = text_layer_box(page, "Forscherin")
    assert box["left"] < drawn["x"] < box["right"] and box["top"] - 2 < drawn["y"] < box["bottom"] + 2, (drawn, box)
    # The PDF has "Gr¨uße" and "K¨oln" (TeX's OT1 fonts), the source "Grüße" and "Köln".
    drag_in_pdf(page, "Gr", "ln")
    wait_until(lambda: picked(page) == "Grüße aus Köln", timeout=10, what="the passage with umlauts marked")
    # A click elsewhere ends the selection, and the marks go.
    box = page.locator(".rv-pdf-col .pdfview").bounding_box()
    page.mouse.click(box["x"] + 6, box["y"] + 6)
    expect(page.locator(".rv-pick")).to_have_count(0)
    expect(float_btn).to_be_hidden()
    ctx.close()


@test
def pdf_of_the_changes(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    ctx = env.context(browser, "bob")
    page = env.go(env.page(ctx, "bob"), f"/reviews/{env.review}/changes")
    page.get_by_role("button", name="PDF of the changes").click()
    expect(page.locator(".rv-pdfdiff-host .pdfview-page").first).to_be_visible(timeout=180000)
    env.shot(page, "changes-pdf-1280")
    ctx.close()


# --- tests: comments in the editor (step 3) -----------------------------------------------------------


def open_editor(env, page, path="main.tex"):
    env.go(page, f"/edit/{path}")
    expect(page.locator(".editor-status")).to_have_text("Connected", timeout=10000)
    expect(page.locator(".cm-content")).to_be_visible()
    return page


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


def cm_select(page, needle, collapse=False):
    """Select `needle` in the open file (or put the cursor in front of it) and focus the text."""
    cm_eval(page, """
        const text = view.state.doc.toString();
        const at = text.indexOf(arg.needle);
        if (at < 0) throw new Error(`${arg.needle} is not in the file`);
        view.focus();
        view.dispatch({ selection: { anchor: at, head: arg.collapse ? at : at + arg.needle.length } });
    """, {"needle": needle, "collapse": collapse})


def mark_text(page, thread_id):
    return page.evaluate(
        "(id) => [...document.querySelectorAll(`.cm-rv-thread[data-thread='${id}']`)].map((m) => m.textContent).join('')",
        thread_id,
    )


@test
def editor_highlights_and_panel(env, browser):
    text = env.file("alice", "main.tex")
    env.put("alice", "main.tex", text.replace("The results are good.", "🧪 The results are good."))
    ctx = env.context(browser, "alice")
    page = open_editor(env, env.page(ctx, "alice"))
    button = page.locator(".editor-comments-btn")
    # The reopened greeting, the paragraph and the outdated single comment are open.
    expect(button.locator(".counter")).to_have_text("3")
    wait_until(lambda: mark_text(page, env.threads["greeting"]) == "Grüße aus dem schönen Köln", what="the greeting's highlight")
    assert mark_text(page, env.threads["paragraph"]).startswith("This paragraph explains")
    expect(page.locator(f".cm-rv-thread[data-thread='{env.threads['suggestion']}']")).to_have_count(0)

    button.click()
    panel = page.locator(".editor-comments")
    expect(panel).to_be_visible()
    expect(page.locator(".pdf-pane")).to_be_hidden()
    expect(panel.locator(".editor-comments-list > .rv-thread")).to_have_count(3)
    expect(panel.locator(".editor-comments-more")).to_contain_text("1 accepted comment")
    # A card leads to its text.
    card(page, env.threads["paragraph"]).locator(".rv-thread-head").click()
    expect(page.locator(f".cm-rv-thread.is-selected[data-thread='{env.threads['paragraph']}']").first).to_be_visible()

    # Typing in front of a highlight leaves it on its words.
    cm_select(page, "Grüße aus dem schönen Köln", collapse=True)
    page.keyboard.type("Liebe ")
    expect(page.locator(".editor-comment-float")).to_be_hidden()
    assert mark_text(page, env.threads["greeting"]) == "Grüße aus dem schönen Köln", mark_text(page, env.threads["greeting"])
    wait_until(lambda: "Liebe Grüße" in (env.file("alice", "main.tex") or ""), what="the typing on the server")

    # A comment on a selection after an emoji, so UTF-16 and code points differ.
    cm_select(page, "results are good")
    float_btn = page.locator(".editor-comment-float")
    expect(float_btn).to_be_visible()
    env.shot(page, "editor-selection-1280")
    float_btn.click()
    compose = panel.locator(".editor-compose")
    expect(compose.locator(".rv-compose-head")).to_have_text("Comment on line 8")
    expect(compose.locator(".rv-compose-quote")).to_have_text("results are good")
    compose.get_by_label("Comment").fill("Which results? Grüße 🎉")
    compose.get_by_role("button", name="Add single comment").click()
    expect(compose).to_have_count(0)
    with env.api("alice") as c:
        made = [t for t in c.get(env.url("/threads")).json() if t["quote"] == "results are good"]
    assert len(made) == 1 and made[0]["comments"][0]["body"] == "Which results? Grüße 🎉", made
    env.threads["editor"] = made[0]["id"]
    wait_until(lambda: mark_text(page, made[0]["id"]) == "results are good", what="the new comment's highlight")
    expect(button.locator(".counter")).to_have_text("4")

    # Resolving in the panel marks the highlight as resolved.
    paragraph = card(page, env.threads["paragraph"])
    paragraph.get_by_role("button", name="Resolve conversation").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Note").fill("Shortened.")
    dialog.get_by_role("button", name="Resolve conversation").click()
    expect(paragraph.locator(".rv-labels")).to_contain_text("Resolved")
    expect(page.locator(f".cm-rv-thread.is-resolved[data-thread='{env.threads['paragraph']}']").first).to_be_visible()
    env.shot(page, "editor-comments-1280")
    # The PDF button gives the column back to the PDF.
    page.locator(".editor-toolbar").get_by_role("button", name="PDF", exact=True).click()
    expect(panel).to_be_hidden()
    expect(page.locator(".pdf-pane")).to_be_visible()
    ctx.close()


@test
def editor_comments_for_reviewers_and_viewers(env, browser):
    # A reviewer cannot type, but comments, and starts a review on the text as it is.
    ctx = env.context(browser, "rita")
    page = open_editor(env, env.page(ctx, "rita"))
    cm_select(page, "We conclude" if "We conclude" in env.file("rita", "main.tex") else "Conclusion")
    page.locator(".editor-comment-float").click()
    compose = page.locator(".editor-compose")
    compose.get_by_label("Comment").fill("Pending note from Rita.")
    compose.get_by_role("button", name="Start a review").click()
    expect(compose).to_have_count(0)
    with env.api("rita") as c:
        mine = [t for t in c.get(env.url("/threads")).json() if t["comments"][0]["body"] == "Pending note from Rita."]
    assert len(mine) == 1 and mine[0]["pending"], mine
    pending = mine[0]["id"]
    expect(card(page, pending).locator(".rv-labels")).to_contain_text("Pending")
    expect(page.locator(f".cm-rv-thread.is-pending[data-thread='{pending}']").first).to_be_visible()
    ctx.close()

    # Nobody else sees it.
    ctx = env.context(browser, "bob")
    page = open_editor(env, env.page(ctx, "bob"))
    wait_until(lambda: mark_text(page, env.threads["greeting"]), what="the highlights")
    expect(page.locator(f".cm-rv-thread[data-thread='{pending}']")).to_have_count(0)
    ctx.close()

    # A viewer reads the comments and cannot write one.
    ctx = env.context(browser, "vic", width=390, height=844)
    page = open_editor(env, env.page(ctx, "vic"))
    cm_select(page, "results are good")
    page.wait_for_timeout(300)
    expect(page.locator(".editor-comment-float")).to_be_hidden()
    page.locator(".editor-comments-btn").click()
    panel = page.locator(".editor-comments")
    expect(panel).to_be_visible()
    expect(page.locator(".editor-main")).to_be_hidden()
    expect(panel.locator(".rv-thread").first).to_be_visible()
    expect(panel.get_by_role("button", name="Reply…")).to_have_count(0)
    no_page_scroll(page, 390)
    env.shot(page, "editor-comments-390")
    panel.get_by_label("Hide the comments").click()
    expect(page.locator(".editor-main")).to_be_visible()
    ctx.close()


# --- tests: the compile's problems and the PDF's spot in the editor ----------------------------------------

PROBLEMS = r"""\documentclass{article}
\begin{document}
\input{intro}
A reference to \ref{nowhere} that does not exist.

Second line \undefinedcommand{} here.
\end{document}
"""

SPOT = r"""\documentclass{article}
\begin{document}
The first alpha comes early in this sentence, and much later the second alpha arrives with different words around it.

Die na\"ive Forscherin trinkt Kaffee.
\end{document}
"""


def paper_with(env, title, files, members=None):
    paper = env.create_paper("alice", title, members=members)
    with env.api("alice") as c:
        for path, body in files.items():
            c.put(f"/api/projects/{paper['id']}/files/{path}", content=body.encode()).raise_for_status()
        c.delete(f"/api/projects/{paper['id']}/files/refs.bib").raise_for_status()
    return paper


def problem_line(page, level):
    return page.locator(f".cm-problemLine.is-{level}")


@test
def editor_marks_compile_problems(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    paper = paper_with(env, f"Problems {env.run}", {"main.tex": PROBLEMS, "intro.tex": "Intro text with \\alsoundefined{} in it.\n"},
                       members={"vic": "viewer"})
    ctx = env.context(browser, "alice")
    # Compiles only when asked, so the lines stay those of the first compile.
    ctx.add_init_script("try { localStorage.setItem('meertex.editor.autocompile', 'off'); } catch {}")
    page = env.page(ctx, "alice")
    page.goto(f"{env.base}/p/{paper['name']}/edit/main.tex")
    expect(page.locator(".pdf-status")).to_contain_text("error", timeout=120000)
    error = problem_line(page, "error")
    warning = problem_line(page, "warning")
    expect(error).to_have_count(1)
    expect(error).to_contain_text("\\undefinedcommand")
    expect(warning).to_have_count(1)
    expect(warning).to_contain_text("\\ref{nowhere}")
    expect(page.locator(".cm-gutter-lint .cm-lint-marker-error")).to_have_count(1)
    expect(page.locator(".cm-gutter-lint .cm-lint-marker-warning")).to_have_count(1)
    amber = warning.evaluate("(el) => getComputedStyle(el).backgroundColor")
    assert amber.startswith("rgba(255, 241, 229"), amber
    # The message on hovering the gutter's icon, and the text.
    page.locator(".cm-lint-marker-error").hover()
    expect(page.locator(".cm-tooltip-lint")).to_contain_text("Undefined control sequence")
    page.mouse.move(5, 5)
    expect(page.locator(".cm-tooltip-lint")).to_have_count(0)
    page.locator(".cm-lintRange-error").first.hover()
    expect(page.locator(".cm-tooltip-lint")).to_contain_text("Undefined control sequence")
    env.shot(page, "editor-problems-1280")
    page.mouse.move(5, 5)

    # The lines move with an edit, and stay right on another file and back.
    cm_select(page, "\\documentclass", collapse=True)
    page.keyboard.type("% top\n")
    expect(error).to_contain_text("\\undefinedcommand")
    page.locator(".editor-tree .tree-row[data-path='intro.tex'] .tree-link").click()
    expect(page).to_have_url(re.compile(r"/edit/intro\.tex$"))
    expect(error).to_have_count(1)
    expect(error).to_contain_text("\\alsoundefined")
    expect(warning).to_have_count(0)
    page.locator(".editor-tree .tree-row[data-path='main.tex'] .tree-link").click()
    expect(page).to_have_url(re.compile(r"/edit/main\.tex$"))
    expect(error).to_contain_text("\\undefinedcommand")
    expect(warning).to_contain_text("\\ref{nowhere}")

    # Fixing the line and compiling again takes its mark away.
    cm_select(page, "\\undefinedcommand{}")
    page.keyboard.type("fixed")
    wait_until(lambda: "Second line fixed here." in page_text(page), what="the fix in the text")
    page.keyboard.press("Control+Enter")
    expect(error).to_have_count(0, timeout=120000)
    expect(warning).to_have_count(1)
    ctx.close()

    # A viewer sees them too.
    ctx = env.context(browser, "vic")
    page = env.page(ctx, "vic")
    page.goto(f"{env.base}/p/{paper['name']}/edit/intro.tex")
    expect(problem_line(page, "error")).to_contain_text("\\alsoundefined", timeout=120000)
    ctx.close()


def page_text(page):
    return cm_eval(page, "return view.state.doc.toString();")


def pdf_word_point(env, paper, page, needle, occurrence=0, url=None, host=".editor-pdf"):
    """The screen point of the `occurrence`th `needle` on the first page of the
    PDF in `host`, from the PDF's own text. The PDF is the paper's latest
    compile, or `url`."""
    if url is None:
        with env.api("alice") as c:
            latest = c.get(f"/api/projects/{paper['id']}/compiles/latest").json()
        url = (latest or {}).get("pdf_url")
        assert url, latest
    return page.evaluate(
        """async ([needle, occurrence, url, host]) => {
            const lib = await import('/static/vendor/pdfjs/pdf.js');
            lib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.js';
            const pdf = await lib.getDocument({ url }).promise;
            const page = await pdf.getPage(1);
            const base = page.getViewport({ scale: 1 });
            const { items } = await page.getTextContent();
            let seen = 0;
            for (const it of items) {
                for (let i = it.str.indexOf(needle); i >= 0; i = it.str.indexOf(needle, i + 1)) {
                    if (seen++ < occurrence) continue;
                    const w = it.width / it.str.length;
                    const x = it.transform[4] + (i + needle.length / 2) * w;
                    const y = it.transform[5] + it.height * 0.3;
                    const el = document.querySelector(`${host} .pdfview-page[data-page="1"]`);
                    const r = el.getBoundingClientRect();
                    const [vx, vy] = page.getViewport({ scale: r.width / base.width }).convertToViewportPoint(x, y);
                    return { x: r.left + vx, y: r.top + vy };
                }
            }
            throw new Error(`${needle} #${occurrence} is not in the PDF: ${items.map((it) => it.str).join("|")}`);
        }""",
        [needle, occurrence, url, host],
    )


def selection(page):
    return cm_eval(page, """
        const s = view.state.selection.main;
        return { text: view.state.sliceDoc(s.from, s.to), from: s.from, line: view.state.doc.lineAt(s.from).number };
    """)


@test
def pdf_click_finds_the_word(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    paper = paper_with(env, f"Spot {env.run}", {"main.tex": SPOT})
    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    page.goto(f"{env.base}/p/{paper['name']}/edit/main.tex")
    expect(page.locator(".editor-pdf .pdfview-page").first).to_be_visible(timeout=120000)
    page.wait_for_timeout(500)
    second = SPOT.index("alpha", SPOT.index("alpha") + 1)

    # The second "alpha" of a long line, not the first.
    point = pdf_word_point(env, paper, page, "alpha", 1)
    page.mouse.click(point["x"], point["y"])
    wait_until(lambda: selection(page)["text"] == "alpha", timeout=10, what="the word selected")
    got = selection(page)
    assert got["line"] == 3 and got["from"] == second, (got, second)
    expect(page.locator(".cm-syncWord")).to_have_text("alpha")
    expect(page.locator(".cm-syncLine")).to_have_count(1)
    env.shot(page, "editor-spot-1280")
    # It fades and goes.
    expect(page.locator(".cm-syncLine")).to_have_count(0, timeout=6000)

    # The PDF has "na¨" and "ıve" for "naïve" (TeX's OT1 fonts), the source na\"ive.
    point = pdf_word_point(env, paper, page, "\u0131ve", 0)
    page.mouse.click(point["x"], point["y"])
    wait_until(lambda: selection(page)["text"] == 'na\\"ive', timeout=10, what="the accented word selected")
    assert selection(page)["line"] == 5
    ctx.close()


# --- main -----------------------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8066)
    parser.add_argument("--db", default=os.environ.get("MEERTEX_REVIEWS_UI_DB", DEFAULT_DB))
    parser.add_argument("--shots", help="save screenshots into this folder")
    parser.add_argument("--tmp", help="the folder for the server's data and logs (default: the system's)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder with the server's log")
    parser.add_argument("--compile", action="store_true",
                        help="start the compile service natively with this machine's TeX Live. The PDF tests need one")
    parser.add_argument("--compile-port", type=int, default=8076, help="its port with --compile (8076)")
    parser.add_argument("--compile-url", default=os.environ.get("COMPILE_URL"), help="or use this running compile service")
    parser.add_argument("--compile-token", default=os.environ.get("COMPILE_TOKEN"), help="and its token")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--just", action="store_true", help="run only the named tests, without the ones before them (for tests that make their own paper)")
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
                if args.just and fn.__name__ not in args.only:
                    continue
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
