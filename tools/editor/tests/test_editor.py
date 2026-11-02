"""End-to-end tests of the browser editor (app/static/js/pages/editor.js).

Real browsers (Google Chrome through Playwright) against a real server, a real
database and the real CLI sync client. tools/editor/README.md says how to
run it. Each test is a function below. A run prints one line per test and
exits 1 when one failed.
"""

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

REPO = Path(__file__).resolve().parents[3]
PYTHON = REPO / ".venv" / "bin" / "python"
MEERTEX = REPO / ".venv" / "bin" / "meertex"
DEFAULT_DB = "postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_editor"
# A 2x2 red PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
)

TESTS = []
# Tests that restart the server. They run after the idle test, which needs one
# unbroken connection.
LATE = []


def test(fn):
    TESTS.append(fn)
    return fn


def late_test(fn):
    LATE.append(fn)
    return fn


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


class FakeLanguageTool(BaseHTTPRequestHandler):
    """Just enough of LanguageTool's HTTP API (v2) for the editor's checker.
    Every "teh" in a text item of the annotated text is a spelling mistake, at
    its UTF-16 offset in the items' text and markup joined, as LanguageTool
    counts. The server this script starts has GRAMMAR_URL pointing here."""

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path != "/v2/languages":
            self.send_error(404)
            return
        self.reply([{"name": "English (US)", "code": "en", "longCode": "en-US"},
                    {"name": "English (GB)", "code": "en", "longCode": "en-GB"},
                    {"name": "German (Germany)", "code": "de", "longCode": "de-DE"}])

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/v2/check":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        form = urllib.parse.parse_qs(body, keep_blank_values=True)
        items = json.loads(form["data"][0])["annotation"] if "data" in form else [{"text": form.get("text", [""])[0]}]
        matches = []
        at = 0
        for item in items:
            if "text" not in item:
                at += utf16(item.get("markup", ""))
                continue
            for m in re.finditer(r"\bteh\b", item["text"]):
                matches.append({
                    "message": "Possible spelling mistake found.", "shortMessage": "Spelling mistake",
                    "replacements": [{"value": "the"}], "offset": at + utf16(item["text"][:m.start()]), "length": 3,
                    "rule": {"id": "MORFOLOGIK_RULE_EN_US", "description": "Possible spelling mistake",
                             "issueType": "misspelling", "category": {"id": "TYPOS", "name": "Possible Typo"}},
                })
            at += utf16(item["text"])
        english = {"name": "English (US)", "code": "en-US"}
        self.reply({"software": {"name": "LanguageTool", "version": "6.8"},
                    "language": {**english, "detectedLanguage": {**english, "confidence": 0.9}}, "matches": matches})

    def log_message(self, *args):
        pass


class Env:
    """The server, the people and the papers every test uses."""

    def __init__(self, args):
        self.args = args
        self.port = args.port
        self.base = f"http://127.0.0.1:{self.port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="meertex-editor-", dir=args.tmp))
        self.server = None
        self.run = secrets.token_hex(3)
        self.shots = Path(args.shots) if args.shots else None
        self.errors = []
        # Set while the server is down on purpose, because the browser logs every refused reconnect.
        self.server_down = False
        # The PDF tests need a compile service (docs/DESIGN.md §14).
        self.compile_service = None
        self.compiling = bool(args.compile or args.compile_url or args.server)
        # The spelling and grammar checker the server asks (FakeLanguageTool).
        self.languagetool = None

    # --- server --------------------------------------------------------------

    def start_languagetool(self):
        """FakeLanguageTool in a thread, on a free port. It outlives a restart of the server."""
        self.languagetool = ThreadingHTTPServer(("127.0.0.1", 0), FakeLanguageTool)
        threading.Thread(target=self.languagetool.serve_forever, daemon=True).start()

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
        if self.args.server:
            self.base = self.args.server.rstrip("/")
            return
        if self.args.compile and not self.compile_service:
            self.start_compile_service()
        if not self.languagetool:
            self.start_languagetool()
        env = {
            **os.environ,
            "DATABASE_URL": self.args.db,
            "DATA_DIR": str(self.tmp / "data"),
            "BASE_URL": self.base,
            "SMTP_HOST": "",
            "COMPILE_URL": self.args.compile_url or "",
            "COMPILE_TOKEN": self.args.compile_token or "",
            "GRAMMAR_URL": f"http://127.0.0.1:{self.languagetool.server_address[1]}",
        }
        # The server gets its own copy of the log's file descriptor.
        with open(self.tmp / "server.log", "a") as log:
            self.server = subprocess.Popen(
                [str(REPO / ".venv" / "bin" / "uvicorn"), "app.main:app", "--host", "127.0.0.1",
                 "--port", str(self.port), "--ws-max-size", "100000000"],
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

    def stop_server(self):
        if self.server and self.server.poll() is None:
            self.server.send_signal(signal.SIGINT)
            try:
                self.server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.server.kill()

    def stop(self):
        self.stop_server()
        if self.compile_service and self.compile_service.poll() is None:
            self.compile_service.terminate()
            try:
                self.compile_service.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.compile_service.kill()
        if self.languagetool:
            self.languagetool.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- people and papers --------------------------------------------------------

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
            "carol": (f"carol-{r}@example.com", "Carol Chen"),
            "dave": (f"dave-{r}@example.com", "Dave Dunn"),
        }
        made = self.make_users(self.people.values())
        self.session = {k: made[e][0] for k, (e, _) in self.people.items()}
        self.token = {k: made[e][1] for k, (e, _) in self.people.items()}
        self.paper = self.create_paper("alice", f"Editor test {r}", members={"bob": "editor", "carol": "viewer"})
        # Dave's paper is for the idle test, where nobody else may be connected.
        self.quiet = self.create_paper("dave", f"Quiet paper {r}")

    def api(self, who):
        return httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {self.token[who]}"}, timeout=10)

    def browser_api(self, who):
        """With the session cookie instead of the token, for the routes only a
        browser may use, /api/grammar/ among them."""
        return httpx.Client(base_url=self.base, cookies={"meertex_session": self.session[who]}, timeout=10)

    def create_paper(self, owner, title, members=None):
        with self.api(owner) as c:
            p = c.post("/api/projects", json={"title": title, "conference": "TEST", "year": 2027, "starter": "basic"})
            p.raise_for_status()
            project = p.json()
            for who, role in (members or {}).items():
                c.post(f"/api/projects/{project['id']}/members", json={"email": self.people[who][0], "role": role}).raise_for_status()
        return project

    def file(self, who, path, project=None):
        project = project or self.paper
        with self.api(who) as c:
            r = c.get(f"/api/projects/{project['id']}/files/{path}")
        return r.text if r.status_code == 200 else None

    def put(self, who, path, body, project=None):
        project = project or self.paper
        with self.api(who) as c:
            c.put(f"/api/projects/{project['id']}/files/{path}", content=body).raise_for_status()

    # --- browsers -----------------------------------------------------------------

    def context(self, browser, who, width=1280, height=860):
        ctx = browser.new_context(viewport={"width": width, "height": height})
        ctx.add_cookies([{"name": "meertex_session", "value": self.session[who], "url": self.base}])
        return ctx

    def page(self, ctx, label):
        page = ctx.new_page()
        page.sockets = []
        page.on("websocket", lambda ws: page.sockets.append(ws))
        page.on("console", lambda m: m.type == "error" and not self.expected(m) and self.errors.append(f"{label}: {m.text}"))
        page.on("pageerror", lambda e: self.errors.append(f"{label}: {e}"))
        return page

    def expected(self, message):
        """Errors the browser logs for answers the page expects: refused
        reconnects while the server is down on purpose, the 404 of
        /compiles/latest for a paper that was never compiled, the 503 of
        every compile route when there is no compile service, and the 429 of a
        grammar check while the server is still busy with the one before."""
        if self.server_down and "ERR_CONNECTION_REFUSED" in message.text:
            return True
        url = (message.location or {}).get("url", "")
        if "/api/grammar/check" in url and "429" in message.text:
            return True
        return "/compiles/latest" in url or (not self.compiling and "/compile" in url)

    def open_editor(self, page, path="", project=None):
        project = project or self.paper
        page.goto(f"{self.base}/p/{project['name']}/edit" + (f"/{path}" if path else ""))
        expect(page.locator(".editor-status")).to_have_text("Connected", timeout=10000)
        return page

    def shot(self, page, name):
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(self.shots / f"{name}.png"))


# --- helpers ------------------------------------------------------------------------


def editor_text(page: Page) -> str:
    """The open file as CodeMirror holds it."""
    return page.evaluate(
        """async () => {
            const { EditorView } = await import('/static/vendor/editor/editor.js');
            const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
            return view ? view.state.doc.toString() : null;
        }"""
    )


def cursor_line(page: Page) -> int:
    """The line of the open file the cursor is on."""
    return page.evaluate(
        """async () => {
            const { EditorView } = await import('/static/vendor/editor/editor.js');
            const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
            return view ? view.state.doc.lineAt(view.state.selection.main.head).number : null;
        }"""
    )


def wait_until(fn, timeout=5.0, interval=0.05, what="condition"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what} (last: {last!r})")


def put_cursor_at_end_of(page: Page, needle: str):
    """Click into the editor and move the cursor to the end of the line holding `needle`."""
    line = page.locator(".cm-line", has_text=needle).first
    line.click()
    page.keyboard.press("End")


def open_sockets(page) -> list:
    # Playwright's sync API delivers events, a socket's close among them, only
    # while it runs. wait_until() sleeps between tries, so give it a turn here.
    page.wait_for_timeout(1)
    return [ws for ws in page.sockets if not ws.is_closed()]


# --- tests -----------------------------------------------------------------------------


@test
def typing_reaches_the_server(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"))
    assert page.url.endswith("/edit/main.tex"), page.url
    put_cursor_at_end_of(page, "\\maketitle")
    typed = " Grüße aus Köln, naïve café 🎉🧪 ☕"
    page.keyboard.type(typed)
    t0 = time.monotonic()
    wait_until(lambda: typed in (env.file("alice", "main.tex") or ""), timeout=1.0, what="the server to have the text")
    latency = time.monotonic() - t0
    assert typed in editor_text(page)
    # Ctrl-S is answered by the page, not the browser.
    page.keyboard.press("Control+s")
    expect(page.locator(".editor-note")).to_have_text("Saved automatically")
    # Search, and undo through the document's undo manager.
    page.keyboard.press("Control+f")
    page.keyboard.type("section")
    expect(page.locator(".cm-searchMatch")).not_to_have_count(0)
    env.shot(page, "search-1280")
    page.keyboard.press("Escape")
    page.keyboard.type("UNDO ME")
    wait_until(lambda: "UNDO ME" in (env.file("alice", "main.tex") or ""), what="the text to undo")
    page.keyboard.press("Control+z")
    wait_until(lambda: "UNDO ME" not in (env.file("alice", "main.tex") or ""), what="the undo on the server")
    ctx.close()
    return f"on the server {latency * 1000:.0f} ms after the last key"


@test
def two_browsers_converge(env, browser):
    ca, cb = env.context(browser, "alice"), env.context(browser, "bob")
    a = env.open_editor(env.page(ca, "alice"), "main.tex")
    b = env.open_editor(env.page(cb, "bob"), "main.tex")
    env.put("alice", "main.tex", (env.file("alice", "main.tex") or "") + "\nShared paragraph here.\n")
    for p in (a, b):
        wait_until(lambda p=p: "Shared paragraph here." in editor_text(p), what="the paragraph")
    put_cursor_at_end_of(a, "Shared paragraph here.")
    put_cursor_at_end_of(b, "Shared paragraph here.")
    # Keystrokes alternate between the two browsers, so edits cross in flight.
    for i in range(12):
        a.keyboard.type("a")
        b.keyboard.type("b")
    wait_until(lambda: editor_text(a) == editor_text(b), what="both editors to agree")

    def server_agrees():
        text = env.file("bob", "main.tex")
        return text if text == editor_text(a) else None

    server = wait_until(server_agrees, what="the server to agree")
    line = next(l for l in server.splitlines() if l.startswith("Shared paragraph here."))
    assert line.count("a") - "Shared paragraph here.".count("a") == 12 and line.count("b") == 12, line
    # Each sees the other's cursor with a name, and the other in the toolbar.
    expect(a.locator(".cm-ySelectionInfo", has_text="Bob Brown")).to_have_count(1)
    expect(b.locator(".cm-ySelectionInfo", has_text="Alice Andersson")).to_have_count(1)
    expect(a.locator(".editor-presence")).to_contain_text("Bob Brown")
    expect(b.locator(".editor-presence")).to_contain_text("Alice Andersson")
    env.shot(a, "two-people-1280")
    ca.close()
    # Bob sees Alice leave.
    expect(b.locator(".editor-presence")).to_be_hidden(timeout=5000)
    cb.close()
    return f"converged on {line!r}"


@test
def api_edit_shows_up(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "refs.bib")
    before = env.file("alice", "refs.bib")
    env.put("bob", "refs.bib", before + "@misc{api2027, title = {Written through the API}}\n")
    wait_until(lambda: "Written through the API" in editor_text(page), timeout=2, what="the API edit in the editor")
    assert editor_text(page).startswith(before)
    ctx.close()


@test
def cli_checkout_follows_the_browser(env, browser):
    home = env.tmp / "cli"
    papers = home / "papers"
    cli_env = {
        **os.environ,
        "MEERTEX_HOME": str(home / "meertex"),
        "MEERTEX_PAPERS": str(papers),
        "XDG_CONFIG_HOME": str(home / "config"),
        "MEERTEX_SERVER": env.base,
        "MEERTEX_TOKEN": env.token["alice"],
    }
    subprocess.run([str(MEERTEX), "checkout", env.paper["name"]], env=cli_env, check=True, capture_output=True, text=True)
    local = papers / env.paper["name"] / "main.tex"
    assert local.exists()
    with open(env.tmp / "sync.log", "w") as log:
        sync = subprocess.Popen([str(MEERTEX), "sync"], env=cli_env, stdout=log, stderr=subprocess.STDOUT)
    try:
        ctx = env.context(browser, "bob")
        page = env.open_editor(env.page(ctx, "bob"), "main.tex")
        time.sleep(1.5)  # the sync client connects
        put_cursor_at_end_of(page, "\\maketitle")
        page.keyboard.type(" Typed in the browser, ü.")
        t0 = time.monotonic()
        wait_until(lambda: "Typed in the browser, ü." in local.read_text(), timeout=5, what="the browser edit in the local file")
        to_disk = time.monotonic() - t0
        text = local.read_text()
        local.write_text(text.replace("\\end{document}", "Written on the disk, é.\n\n\\end{document}"))
        t0 = time.monotonic()
        wait_until(lambda: "Written on the disk, é." in editor_text(page), timeout=5, what="the local edit in the browser")
        to_browser = time.monotonic() - t0
        assert editor_text(page) == local.read_text()
        ctx.close()
    finally:
        sync.send_signal(signal.SIGINT)
        try:
            sync.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sync.kill()
    return f"browser to disk {to_disk * 1000:.0f} ms, disk to browser {to_browser * 1000:.0f} ms"


@test
def file_tree(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex")
    tree = page.locator(".editor-tree-body")

    # New file, in a new folder.
    page.get_by_label("Add a file").click()
    page.get_by_role("menuitem", name="New file…").click()
    dialog = page.locator("dialog")
    dialog.get_by_label("Path").fill("sections/intro.tex")
    env.shot(page, "dialog-1280")
    dialog.get_by_role("button", name="Create file").click()
    expect(page).to_have_url(re.compile(r"/edit/sections/intro\.tex$"))
    expect(tree.locator(".tree-row[data-dir='sections']")).to_be_visible()
    wait_until(lambda: editor_text(page) == "\\section{}\n", what="the starter text")
    assert env.file("alice", "sections/intro.tex") == "\\section{}\n"

    # A clash is refused before anything is sent.
    page.get_by_label("Add a file").click()
    page.get_by_role("menuitem", name="New file…").click()
    dialog.get_by_label("Path").fill("main.tex")
    dialog.get_by_role("button", name="Create file").click()
    expect(dialog.locator(".form-error")).to_have_text("main.tex exists already")
    dialog.get_by_role("button", name="Cancel").click()

    # Rename through the row's menu.
    row = tree.locator(".tree-row[data-path='sections/intro.tex']")
    row.hover()
    row.get_by_label("Actions for sections/intro.tex").click()
    row.get_by_role("menuitem", name="Rename or move…").click()
    dialog.get_by_label("New path").fill("sections/introduction.tex")
    dialog.get_by_role("button", name="Rename").click()
    expect(page).to_have_url(re.compile(r"/edit/sections/introduction\.tex$"))
    expect(tree.locator(".tree-row[data-path='sections/introduction.tex']")).to_be_visible()
    assert env.file("alice", "sections/intro.tex") is None

    # Someone else renames the open file. The page follows it and keeps typing into it.
    with env.api("bob") as c:
        c.post(f"/api/projects/{env.paper['id']}/files/move", json={"from": "sections", "to": "chapters"}).raise_for_status()
    expect(page).to_have_url(re.compile(r"/edit/chapters/introduction\.tex$"), timeout=3000)
    expect(page.locator(".editor-path")).to_contain_text("introduction.tex")
    page.locator(".cm-content").click()
    page.keyboard.press("Control+End")
    page.keyboard.type("Still the same file.")
    wait_until(lambda: "Still the same file." in (env.file("alice", "chapters/introduction.tex") or ""), what="typing after the rename")

    # Upload through the file picker, and open the image.
    page.locator(".editor-tree-head input[type=file]").set_input_files(
        files=[{"name": "figure.png", "mimeType": "image/png", "buffer": PNG}])
    expect(tree.locator(".tree-row[data-path='figure.png']")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/edit/figure\.png$"))
    img = page.locator(".editor-image img")
    expect(img).to_be_visible()
    wait_until(lambda: img.evaluate("i => i.complete && i.naturalWidth") == 2, what="the image to load")
    env.shot(page, "image-1280")

    # Upload by dropping a file on a folder row.
    dt = page.evaluate_handle("""() => {
        const dt = new DataTransfer();
        dt.items.add(new File(["Dropped notes, ß.\\n"], "notes.txt", { type: "text/plain" }));
        return dt;
    }""")
    folder = tree.locator(".tree-row[data-dir='chapters']")
    folder.dispatch_event("dragover", {"dataTransfer": dt})
    folder.dispatch_event("drop", {"dataTransfer": dt})
    expect(tree.locator(".tree-row[data-path='chapters/notes.txt']")).to_be_visible()
    assert env.file("alice", "chapters/notes.txt") == "Dropped notes, ß.\n"

    # Move by dragging a row onto another folder.
    page.get_by_label("Add a file").click()
    page.get_by_role("menuitem", name="New folder…").click()
    dialog.get_by_label("Folder").fill("figures")
    dialog.get_by_label("Its first file").fill("README.md")
    dialog.get_by_role("button", name="Create folder").click()
    expect(tree.locator(".tree-row[data-path='figures/README.md']")).to_be_visible()
    tree.locator(".tree-row[data-path='figure.png']").drag_to(tree.locator(".tree-row[data-dir='figures']"))
    expect(tree.locator(".tree-row[data-path='figures/figure.png']")).to_be_visible()

    # Delete with a confirmation.
    row = tree.locator(".tree-row[data-path='chapters/notes.txt']")
    row.hover()
    row.get_by_label("Actions for chapters/notes.txt").click()
    row.get_by_role("menuitem", name="Delete").click()
    page.locator("dialog").get_by_role("button", name="Delete file").click()
    expect(tree.locator(".tree-row[data-path='chapters/notes.txt']")).to_have_count(0)
    assert env.file("alice", "chapters/notes.txt") is None

    # Someone else deletes the open file. The page says so and shows the tree.
    tree.locator(".tree-row[data-path='figures/README.md'] a").click()
    expect(page).to_have_url(re.compile(r"/edit/figures/README\.md$"))
    with env.api("bob") as c:
        c.delete(f"/api/projects/{env.paper['id']}/files/figures/README.md").raise_for_status()
    expect(page.locator(".editor-message")).to_contain_text("figures/README.md was deleted")
    env.shot(page, "tree-1280")
    ctx.close()


@test
def viewer_cannot_type(env, browser):
    ctx = env.context(browser, "carol")
    page = env.open_editor(env.page(ctx, "carol"), "main.tex")
    expect(page.locator(".editor-banner")).to_contain_text("read-only")
    env.shot(page, "viewer-1280")
    before = env.file("carol", "main.tex")
    page.locator(".cm-content").click()
    page.keyboard.type("viewer was here")
    page.keyboard.press("Enter")
    page.keyboard.press("Backspace")
    time.sleep(0.8)
    assert "viewer was here" not in editor_text(page)
    assert env.file("carol", "main.tex") == before
    assert page.locator(".editor-tree-head").get_by_label("Add a file").count() == 0
    ctx.close()


@test
def back_forward_and_leaving(env, browser):
    env.put("alice", "back/forward.tex", "\\section{Back and forward}\n")
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex")
    tree = page.locator(".editor-tree-body")
    tree.locator(".tree-row[data-path='refs.bib'] a").click()
    expect(page).to_have_url(re.compile(r"/edit/refs\.bib$"))
    tree.locator(".tree-row[data-path='back/forward.tex'] a").click()
    expect(page).to_have_url(re.compile(r"/edit/back/forward\.tex$"))
    wait_until(lambda: "Back and forward" in (editor_text(page) or ""), what="back/forward.tex in the editor")
    page.go_back()
    expect(page).to_have_url(re.compile(r"/edit/refs\.bib$"))
    wait_until(lambda: editor_text(page) == env.file("alice", "refs.bib"), what="refs.bib in the editor")
    page.go_back()
    expect(page).to_have_url(re.compile(r"/edit/main\.tex$"))
    wait_until(lambda: "\\documentclass" in (editor_text(page) or ""), what="main.tex in the editor")
    page.go_forward()
    expect(page).to_have_url(re.compile(r"/edit/refs\.bib$"))
    assert len(page.sockets) == 1, f"{len(page.sockets)} sockets for one visit"
    # Leaving the page closes the socket.
    page.locator(".underline-nav-item", has_text="Overview").click()
    expect(page.locator(".layout-repo")).to_be_visible()
    wait_until(lambda: not open_sockets(page), timeout=3, what="the socket to close")
    # Back into the editor, and out again with Forward.
    page.go_back()
    expect(page.locator(".editor-status")).to_have_text("Connected")
    assert len(open_sockets(page)) == 1
    page.go_forward()
    expect(page.locator(".layout-repo")).to_be_visible()
    wait_until(lambda: not open_sockets(page), timeout=3, what="the socket to close after Forward")
    ctx.close()


@test
def leaving_stops_every_timer(env, browser):
    """The provider, the awareness and the page run intervals. None outlives the page."""
    ctx = env.context(browser, "alice")
    ctx.add_init_script("""
        const running = new Set();
        const set = window.setInterval;
        const stop = window.clearInterval;
        window.setInterval = function (...args) { const id = set.apply(this, args); running.add(id); return id; };
        window.clearInterval = function (id) { running.delete(id); return stop.call(this, id); };
        window.runningIntervals = () => running.size;
    """)
    page = env.page(ctx, "alice")
    page.goto(f"{env.base}/p/{env.paper['name']}")
    expect(page.locator(".layout-repo")).to_be_visible()
    before = page.evaluate("runningIntervals()")
    page.locator(".underline-nav-item", has_text="Editor").click()
    expect(page.locator(".editor-status")).to_have_text("Connected")
    during = page.evaluate("runningIntervals()")
    page.locator(".underline-nav-item", has_text="Overview").click()
    expect(page.locator(".layout-repo")).to_be_visible()
    wait_until(lambda: not open_sockets(page), timeout=3, what="the socket to close")
    after = page.evaluate("runningIntervals()")
    ctx.close()
    assert during > before and after == before, (before, during, after)
    return f"{during - before} intervals while editing, {after - before} after leaving"


@test
def spellcheck_and_phone(env, browser):
    ctx = env.context(browser, "alice", width=390, height=844)
    page = env.open_editor(env.page(ctx, "alice"), "main.tex")
    content = page.locator(".cm-content")
    button = page.get_by_role("button", name="Spellcheck")
    with env.browser_api("alice") as c:
        checker = c.get("/api/grammar/config").json().get("available")
    if checker:
        # With a checker on the server the button switches it for the
        # account, and the browser's own spellchecker stays off.
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(content).to_have_attribute("spellcheck", "false")
        button.click()
        expect(button).to_have_attribute("aria-pressed", "false")
        page.reload()
        expect(page.locator(".editor-status")).to_have_text("Connected")
        expect(button).to_have_attribute("aria-pressed", "false")
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(content).to_have_attribute("spellcheck", "false")
    # Without one it is the browser's spellchecker, remembered in this browser.
    page.route("**/api/grammar/config", lambda route: route.fulfill(json={
        "available": False, "enabled": True, "language": "auto", "variants": [], "picky": False, "words": [], "disabled_rules": []}))
    page.reload()
    expect(page.locator(".editor-status")).to_have_text("Connected")
    expect(content).to_have_attribute("spellcheck", "false")
    button.click()
    expect(content).to_have_attribute("spellcheck", "true")
    page.reload()
    expect(page.locator(".editor-status")).to_have_text("Connected")
    expect(content).to_have_attribute("spellcheck", "true")
    button.click()
    expect(content).to_have_attribute("spellcheck", "false")
    expect(button).to_have_attribute("aria-pressed", "false")
    # The tree folds away on a phone and opens with the Files button.
    expect(page.locator(".editor-tree")).to_be_hidden()
    env.shot(page, "editor-390")
    page.get_by_role("button", name="Files").click()
    expect(page.locator(".editor-tree")).to_be_visible()
    env.shot(page, "tree-390")
    page.locator(".editor-tree-body .tree-row[data-path='refs.bib'] a").click()
    expect(page.locator(".editor-tree")).to_be_hidden()
    expect(page).to_have_url(re.compile(r"/edit/refs\.bib$"))
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    ctx.close()


@test
def tree_width(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex")
    tree = page.locator(".editor-tree")
    divider = page.get_by_role("separator", name="Width of the file tree")
    usual = tree.bounding_box()["width"]

    def width_is(px):
        wait_until(lambda: abs(tree.bounding_box()["width"] - px) <= 1, what=f"the tree {px:.0f} px wide")

    box = divider.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 120, y, steps=5)
    page.mouse.up()
    width_is(usual + 120)
    # The width is kept, and the divider stays when the PDF is hidden.
    page.reload()
    expect(page.locator(".editor-status")).to_have_text("Connected")
    width_is(usual + 120)
    env.shot(page, "tree-wide-1280")
    page.get_by_role("button", name="PDF", exact=True).click()
    expect(page.locator(".editor-pdf")).to_be_hidden()
    expect(divider).to_be_visible()
    # The arrow keys move it, and a double-click gives the usual width back.
    divider.focus()
    page.keyboard.press("ArrowLeft")
    width_is(usual + 88)
    divider.dblclick()
    width_is(usual)
    page.reload()
    expect(page.locator(".editor-status")).to_have_text("Connected")
    width_is(usual)
    ctx.close()
    return "drag, kept after a reload, arrow keys, double-click"


@test
def outline(env, browser):
    paper = env.create_paper("alice", f"Outline {env.run}")
    main = ("\\documentclass{article}\n\\begin{document}\n\\section{Introduction}\nText.\n"
            "\\input{sections/method}\n% \\section{Commented out}\n\\section{Conclusion}\n\\end{document}\n")
    env.put("alice", "main.tex", main, project=paper)
    env.put("alice", "sections/method.tex", "\\subsection{Setup}\nMore text.\n\\subsection{Results}\nNumbers.\n", project=paper)
    env.put("alice", "letter.tex", "\\section{Response to the reviewers}\n", project=paper)
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex", project=paper)
    box = page.locator(".editor-outline")
    links = box.locator(".tree-link")
    marked = box.locator(".tree-row[aria-current='location']")
    # The main file's headings and those of the file it inputs, in reading order.
    expect(links).to_have_text(["Introduction", "Setup", "Results", "Conclusion"])
    # A click opens the heading's line and marks it. The mark follows the cursor.
    links.filter(has_text="Results").click()
    expect(page).to_have_url(re.compile(r"/edit/sections/method\.tex$"))
    wait_until(lambda: cursor_line(page) == 3, what="the cursor on the heading's line")
    expect(marked).to_have_text("Results")
    page.keyboard.press("Control+Home")
    expect(marked).to_have_text("Setup")
    # A heading typed here and one written by someone else show up.
    page.keyboard.press("Control+End")
    page.keyboard.type("\\subsection{Discussion}")
    page.keyboard.press("Escape")
    expect(links).to_have_text(["Introduction", "Setup", "Results", "Discussion", "Conclusion"])
    expect(marked).to_have_text("Discussion")
    env.put("alice", "main.tex", main.replace("\\section{Conclusion}", "\\section{Related work}\n\\section{Conclusion}"), project=paper)
    expect(links).to_have_text(["Introduction", "Setup", "Results", "Discussion", "Related work", "Conclusion"])
    env.shot(page, "outline-1280")
    # A folded heading hides the ones under it and takes their mark.
    box.get_by_role("button", name="Collapse Introduction").click()
    expect(links).to_have_text(["Introduction", "Related work", "Conclusion"])
    expect(marked).to_have_text("Introduction")
    box.get_by_role("button", name="Expand Introduction").click()
    expect(links).to_have_count(6)
    # The box folds to its header, also after a reload.
    toggle = box.get_by_role("button", name="Outline", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(box.locator(".editor-outline-body")).to_be_hidden()
    page.reload()
    expect(page.locator(".editor-status")).to_have_text("Connected")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(box.locator(".editor-outline-body")).to_be_hidden()
    toggle.click()
    expect(links).to_have_count(6)
    # A .tex file the main file does not read has its own outline.
    page.locator(".editor-tree-body .tree-row[data-path='letter.tex'] a").click()
    expect(links).to_have_text(["Response to the reviewers"])
    ctx.close()
    return "reading order across \\input, click, mark, live edits, folding, kept folded"


@test
def empty_paper_starts_with_new_file(env, browser):
    with env.api("bob") as c:
        p = c.post("/api/projects", json={"title": f"Empty {env.run}", "conference": "TEST", "year": 2027, "main_file": "paper.tex"})
        p.raise_for_status()
        empty = p.json()
    ctx = env.context(browser, "bob")
    page = env.open_editor(env.page(ctx, "bob"), project=empty)
    expect(page.locator(".editor-message")).to_contain_text("No files yet")
    page.locator(".editor-message").get_by_role("button", name="New file").click()
    dialog = page.locator("dialog")
    expect(dialog.get_by_label("Path")).to_have_value("paper.tex")
    dialog.get_by_role("button", name="Create file").click()
    expect(page).to_have_url(re.compile(r"/edit/paper\.tex$"))
    wait_until(lambda: "\\begin{document}" in (editor_text(page) or ""), what="the main file's skeleton")
    ctx.close()


@test
def entry_points_lead_to_the_editor(env, browser):
    with env.api("bob") as c:
        p = c.post("/api/projects", json={"title": f"Fresh {env.run}", "conference": "TEST", "year": 2027})
        p.raise_for_status()
        fresh = p.json()
    ctx = env.context(browser, "bob")
    page = env.page(ctx, "bob")
    page.goto(f"{env.base}/p/{fresh['name']}")
    page.get_by_role("link", name="Write in the browser").click()
    expect(page).to_have_url(re.compile(r"/edit$"))
    expect(page.locator(".editor-message")).to_contain_text("No files yet")
    expect(page.locator(".underline-nav-item", has_text="Editor")).to_have_attribute("aria-current", "page")
    page.goto(f"{env.base}/p/{env.paper['name']}/files/main.tex")
    page.get_by_role("link", name="Open in editor").click()
    expect(page).to_have_url(re.compile(r"/edit/main\.tex$"))
    expect(page.locator(".editor-status")).to_have_text("Connected")
    wait_until(lambda: "\\documentclass" in (editor_text(page) or ""), what="main.tex in the editor")
    ctx.close()


@test
def conflict_copy_is_shown(env, browser):
    """Two entries with one path. The lower id keeps it and the other one
    shows under its conflict name, in the tree and on the server alike."""
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex")
    # A second client writes an entry for main.tex, as an offline coauthor would.
    page.evaluate("""async (pid) => {
        const { Y, WebsocketProvider } = await import('/static/vendor/editor/editor.js');
        const doc = new Y.Doc();
        const p = new WebsocketProvider(`ws://${location.host}/api/projects`, `${pid}/sync`, doc, { disableBc: true });
        await new Promise((r) => p.on('sync', (s) => s && r()));
        doc.getMap('files').set('f'.repeat(32), new Y.Map([['path', 'main.tex'], ['kind', 'text'], ['text', new Y.Text('The other main.tex')]]));
        await new Promise((r) => setTimeout(r, 300));
        p.destroy();
    }""", env.paper["id"])
    row = page.locator(".editor-tree-body .tree-row[data-path='main.conflict-ffffffff.tex']")
    expect(row).to_contain_text("conflict")
    with env.api("alice") as c:
        paths = [f["path"] for f in c.get(f"/api/projects/{env.paper['id']}/files").json()]
    assert "main.conflict-ffffffff.tex" in paths and "main.tex" in paths, paths
    row.locator("a").click()
    expect(page.locator(".editor-path")).to_contain_text("Conflict copy")
    env.shot(page, "conflict-1280")
    wait_until(lambda: editor_text(page) == "The other main.tex", what="the conflict copy in the editor")
    with env.api("alice") as c:
        c.delete(f"/api/projects/{env.paper['id']}/files/main.conflict-ffffffff.tex").raise_for_status()
    expect(row).to_have_count(0)
    ctx.close()


@test
def spelling_and_grammar(env, browser):
    """The server's checker, here FakeLanguageTool: the underline, the
    suggestions, undo, Alt+Shift+G, the dictionary on the Settings page and
    the Spellcheck button."""
    if env.args.server:
        return "skipped, needs the server this script starts"
    paper = env.create_paper("alice", f"Grammar {env.run}")
    env.put("alice", "grammar.tex", "Here is teh first paragraph.\n\nAnd teh second one.\n", project=paper)
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "grammar.tex", project=paper)
    marks = page.locator(".cm-grammar-spelling")
    box = page.locator(".grammar-box")
    button = page.get_by_role("button", name="Spellcheck")
    c = env.browser_api("alice")
    try:
        expect(marks).to_have_count(2, timeout=5000)
        expect(marks.first).to_have_text("teh")
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(button.locator(".counter")).to_have_text("2")
        expect(page.locator(".cm-content")).to_have_attribute("spellcheck", "false")

        # A click on an underline opens the suggestions under it. One fixes the text.
        marks.first.click()
        expect(box).to_be_visible()
        expect(box).to_contain_text("Possible spelling mistake found.")
        env.shot(page, "grammar-box")
        box.get_by_role("button", name="the", exact=True).click()
        expect(box).to_be_hidden()
        wait_until(lambda: "Here is the first" in (env.file("alice", "grammar.tex", project=paper) or ""),
                   what="the suggestion on the server")
        expect(marks).to_have_count(1)
        # Ctrl+Z takes it back like anything typed, and the next check finds the word again.
        page.keyboard.press("Control+z")
        wait_until(lambda: "Here is teh first" in editor_text(page), what="the undo")
        expect(marks).to_have_count(2, timeout=5000)

        # Alt+Shift+G goes to the next underline after the cursor, Escape closes the box.
        page.keyboard.press("Control+Home")
        page.keyboard.press("Alt+Shift+G")
        expect(box).to_be_visible()
        expect(box.get_by_role("button", name="Add to dictionary")).to_be_visible()
        page.keyboard.press("Escape")
        expect(box).to_be_hidden()

        # Add to dictionary takes every "teh" away, for the account.
        marks.last.click()
        box.get_by_role("button", name="Add to dictionary").click()
        expect(marks).to_have_count(0)
        wait_until(lambda: c.get("/api/grammar/config").json()["words"] == ["teh"], what="the word in the dictionary")

        # The Settings page lists it and takes it out again.
        page.goto(f"{env.base}/settings")
        section = page.locator("#grammar")
        expect(section.locator("#grammar-words")).to_contain_text("teh")
        env.shot(section, "grammar-settings")
        section.get_by_role("button", name="Remove teh from your dictionary").click()
        expect(section.locator("#grammar-words .grammar-row")).to_have_count(0)
        assert c.get("/api/grammar/config").json()["words"] == []

        # Back in the editor the word is flagged again. The button turns the checker off and on.
        env.open_editor(page, "grammar.tex", project=paper)
        expect(marks).to_have_count(2, timeout=5000)
        button.click()
        expect(button).to_have_attribute("aria-pressed", "false")
        expect(marks).to_have_count(0)
        assert c.get("/api/grammar/config").json()["enabled"] is False
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(marks).to_have_count(2, timeout=5000)
    finally:
        # The other tests find alice's settings as they were.
        c.put("/api/grammar/config", json={"enabled": True, "words": [], "disabled_rules": []})
        c.close()
        ctx.close()
    return "underline, suggestion, undo, Alt+Shift+G, dictionary, Settings, the switch"


@late_test
def offline_edits_are_kept(env, browser):
    """The server goes away and comes back. What was typed meanwhile arrives."""
    if not env.server:
        return "skipped, needs the server this script starts"
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex")
    env.server_down = True
    env.stop_server()
    expect(page.locator(".editor-status")).to_have_text(re.compile(r"^Offline, changes are kept"), timeout=10000)
    put_cursor_at_end_of(page, "\\maketitle")
    page.keyboard.type(" Typed while offline.")
    env.shot(page, "offline-1280")
    env.start()
    expect(page.locator(".editor-status")).to_have_text("Connected", timeout=15000)
    wait_until(lambda: "Typed while offline." in (env.file("alice", "main.tex") or ""), what="the offline edit on the server")
    ctx.close()
    env.server_down = False


BIB = """@inproceedings{smith2026energy,
  author = {Smith, Jane and M{\\"u}ller, Hans and Doe, John},
  title = {{Energy} Use of Large Language Models},
  year = 2026,
}
@article{lee2025green,
  title = "Green Software in Practice",
  author = "Lee, Kim",
  year = {2025}
}
"""


def completions(page: Page) -> list[str]:
    """The labels of the open completion list."""
    page.wait_for_timeout(250)
    return page.locator(".cm-tooltip-autocomplete .cm-completionLabel").all_inner_texts()


@test
def autocomplete(env, browser):
    paper = env.create_paper("alice", f"Completions {env.run}")
    env.put("alice", "refs.bib", BIB, project=paper)
    env.put("alice", "sections/method.tex", "\\section{Method}\\label{sec:method}\n"
            "% \\label{commented:out}\n\\newcommand{\\sysname}{GreenLM}\n", project=paper)
    env.put("alice", "figures/plot.png", PNG, project=paper)
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex", project=paper)
    put_cursor_at_end_of(page, "\\section{Introduction}")
    page.keyboard.press("Enter")

    # Labels from every .tex file, commented ones left out.
    page.keyboard.type("See \\ref{")
    assert completions(page) == ["sec:method"], completions(page)
    page.keyboard.press("Enter")
    # Keys from the .bib, with the title next to them.
    page.keyboard.type("} and \\citep{smi")
    assert completions(page) == ["smith2026energy"], completions(page)
    expect(page.locator(".cm-completionDetail")).to_have_text("Energy Use of Large Language Models")
    page.keyboard.press("Enter")
    page.keyboard.type("}.")
    # A command with its snippet opens the list of paths.
    page.keyboard.press("Enter")
    page.keyboard.type("\\includegr")
    assert completions(page)[0] == "\\includegraphics", completions(page)
    page.keyboard.press("Enter")
    assert completions(page) == ["figures/plot.png"], completions(page)
    page.keyboard.press("Enter")
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    page.keyboard.type("\\input{")
    assert "sections/method" in completions(page), completions(page)
    page.keyboard.press("Escape")
    page.keyboard.type("sections/method}")
    page.keyboard.press("Enter")
    # \begin{ completes the environment with its body and \end.
    page.keyboard.type("\\begin{ite")
    assert completions(page)[0] == "itemize", completions(page)
    page.keyboard.press("Enter")
    page.keyboard.type("First point")
    # The paper's own commands.
    page.keyboard.press("Control+End")
    page.keyboard.press("Enter")
    page.keyboard.type("\\sysn")
    assert completions(page) == ["\\sysname"], completions(page)
    page.keyboard.press("Escape")
    # A label written elsewhere shows up without a reload.
    method = env.file("alice", "sections/method.tex", project=paper)
    env.put("alice", "sections/method.tex", method + "\\label{tab:results}\n", project=paper)
    page.wait_for_timeout(300)
    page.keyboard.type(" \\cref{")
    assert completions(page) == ["sec:method", "tab:results"], completions(page)
    env.shot(page, "autocomplete-1280")
    page.keyboard.press("Escape")

    def typed():
        text = env.file("alice", "main.tex", project=paper)
        return text if "First point" in text else None

    text = wait_until(typed, what="the completed text on the server")
    for piece in ["See \\ref{sec:method} and \\citep{smith2026energy}.",
                  "\\includegraphics[width=\\linewidth]{figures/plot.png}",
                  "\\input{sections/method}",
                  "\\begin{itemize}\n  \\item First point\n\\end{itemize}"]:
        assert piece in text, (piece, text)
    ctx.close()
    return "labels, keys, paths, environments and the paper's commands"


def select_text(page: Page, needle: str, collapse=False):
    """Select `needle` in the open file, or put the cursor right after it."""
    page.evaluate(
        """async ([needle, collapse]) => {
            const { EditorView } = await import('/static/vendor/editor/editor.js');
            const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
            const from = view.state.doc.toString().indexOf(needle);
            if (from < 0) throw new Error(`${needle} is not in the text`);
            const to = from + needle.length;
            view.dispatch({ selection: collapse ? { anchor: to } : { anchor: from, head: to } });
            view.focus();
        }""",
        [needle, collapse],
    )


@test
def formatting(env, browser):
    env.put("alice", "format.tex", "Energy matters.\nSee the results\n\nfirst line\nsecond line\n\nTitle here\n")
    env.put("alice", "notes.md", "# Notes\n")
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "format.tex")
    bar = page.locator(".editor-format")
    expect(bar).to_be_visible()
    text = lambda: editor_text(page)

    # Ctrl+B wraps the selection and takes the bold away again. Undo goes back.
    select_text(page, "matters")
    page.keyboard.press("Control+b")
    wait_until(lambda: "Energy \\textbf{matters}." in text(), what="\\textbf")
    page.keyboard.press("Control+b")
    wait_until(lambda: text().startswith("Energy matters."), what="the bold to go")
    # Ctrl+I writes \emph, and the button takes it away, also from a cursor inside.
    page.keyboard.press("Control+i")
    wait_until(lambda: "\\emph{matters}" in text(), what="\\emph")
    select_text(page, "\\emph{mat", collapse=True)
    bar.get_by_label("Emphasis").click()
    wait_until(lambda: text().startswith("Energy matters."), what="the emphasis to go")
    # Without a selection the command goes at the cursor with the cursor in its braces.
    select_text(page, "Energy", collapse=True)
    page.keyboard.press("Control+e")
    page.keyboard.type("x")
    wait_until(lambda: text().startswith("Energy\\texttt{x} matters."), what="\\texttt at the cursor")
    page.keyboard.press("Control+z")
    wait_until(lambda: text().startswith("Energy matters."), what="the undo")
    select_text(page, "results")
    # A keyboard sends a capital M with Shift held. Playwright's "m" would not.
    page.keyboard.press("Control+Shift+M")
    wait_until(lambda: "See the $results$" in text(), what="inline math")
    # A citation is tied to the word before, as the papers write it.
    select_text(page, "$results$", collapse=True)
    bar.get_by_label("Insert").click()
    page.get_by_role("menuitem", name="Citation").click()
    wait_until(lambda: "See the $results$~\\cite{}" in text(), what="the citation")

    # Lines become a list, the list changes its kind, and goes back to lines.
    select_text(page, "first line\nsecond line")
    bar.get_by_label("Bulleted list").click()
    listed = "\\begin{itemize}\n  \\item first line\n  \\item second line\n\\end{itemize}"
    wait_until(lambda: listed in text(), what="the itemize")
    select_text(page, listed)
    bar.get_by_label("Numbered list").click()
    wait_until(lambda: listed.replace("itemize", "enumerate") in text(), what="the enumerate")
    select_text(page, listed.replace("itemize", "enumerate"))
    bar.get_by_label("Numbered list").click()
    wait_until(lambda: "\n\nfirst line\nsecond line\n\n" in text(), what="the plain lines")

    # A line becomes a heading, changes its level and goes back to text.
    select_text(page, "Title", collapse=True)
    bar.get_by_label("Heading").click()
    env.shot(page, "format-heading-1280")
    page.get_by_role("menuitemradio", name="Section \\section", exact=True).click()
    wait_until(lambda: "\\section{Title here}" in text(), what="the section")
    bar.get_by_label("Heading").click()
    expect(page.get_by_role("menuitemradio", name="Section \\section", exact=True)).to_have_attribute("aria-checked", "true")
    page.get_by_role("menuitemradio", name="Subsection \\subsection", exact=True).click()
    wait_until(lambda: "\\subsection{Title here}" in text(), what="the subsection")
    bar.get_by_label("Heading").click()
    page.get_by_role("menuitemradio", name="Plain text").click()
    wait_until(lambda: text().endswith("\nTitle here\n"), what="the heading to go")
    # Ctrl+/ comments the line out, and the button comments it in again.
    page.keyboard.press("Control+/")
    wait_until(lambda: "% Title here" in text(), what="the comment")
    bar.get_by_label("Comment the lines out, or in again").click()
    wait_until(lambda: text().endswith("\nTitle here\n"), what="the line back")
    env.shot(page, "format-1280")

    final = "Energy matters.\nSee the $results$~\\cite{}\n\nfirst line\nsecond line\n\nTitle here\n"
    wait_until(lambda: env.file("alice", "format.tex") == final, what="the server to have the formatted text")

    bar.get_by_label("Keyboard shortcuts").click()
    dialog = page.get_by_role("dialog", name="Keyboard shortcuts")
    expect(dialog).to_contain_text("Emphasis, \\emph")
    expect(dialog.locator("kbd").first).to_have_text("Ctrl")
    env.shot(page, "format-shortcuts-1280")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)

    # Other files, and people who may not edit, get no bar.
    page.locator(".editor-tree a.tree-link", has_text="notes.md").click()
    expect(page.locator(".editor-path")).to_contain_text("notes.md")
    expect(bar).to_be_hidden()
    ctx.close()
    ctx = env.context(browser, "carol")
    page = env.open_editor(env.page(ctx, "carol"), "format.tex")
    expect(page.locator(".editor-format")).to_be_hidden()
    ctx.close()
    return "bold, emphasis, monospace, math, citation, lists, headings, comments"


PAPER = r"""\documentclass{article}
\begin{document}
\section{Introduction}
Energy use of large language models is a growing concern.

Forward search lands on this paragraph, which is long enough to wrap onto a
second line of the page so that SyncTeX has a box to show.
\end{document}
"""


def cursor_line(page: Page) -> int:
    return page.evaluate(
        """async () => {
            const { EditorView } = await import('/static/vendor/editor/editor.js');
            const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
            return view.state.doc.lineAt(view.state.selection.main.head).number;
        }"""
    )


def compile_paper(env, title):
    paper = env.create_paper("alice", title)
    env.put("alice", "main.tex", PAPER, project=paper)
    with env.api("alice") as c:
        c.delete(f"/api/projects/{paper['id']}/files/refs.bib")
    return paper


@test
def pdf_pane(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    paper = compile_paper(env, f"Compiling {env.run}")
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex", project=paper)
    status = page.locator(".pdf-status")
    expect(status).to_have_text(re.compile(r"^Compiled in"), timeout=90000)
    expect(page.locator(".pdfview-page")).to_have_count(1)
    expect(page.locator(".pdf-pages")).to_have_text("1 / 1")

    # Typing compiles on its own, two seconds after the last key.
    posts = []
    page.on("request", lambda r: r.method == "POST" and r.url.endswith("/compile") and posts.append(time.monotonic()))
    put_cursor_at_end_of(page, "growing concern.")
    page.keyboard.type(" \\newpage Second page.")
    typed = time.monotonic()
    expect(page.locator(".pdfview-page")).to_have_count(2, timeout=60000)
    assert posts and posts[0] - typed > 1.5, f"compiled {posts[0] - typed:.1f} s after the last key"
    expect(page.locator(".pdf-pages")).to_contain_text("/ 2")
    env.shot(page, "pdf-1280")

    # An error goes to the problems list, and clicking it opens its line.
    put_cursor_at_end_of(page, "has a box to show.")
    page.keyboard.press("Enter")
    page.keyboard.type("\\undefinedcommand")
    error_line = cursor_line(page)
    page.keyboard.press("Control+Enter")
    problem = page.locator(".pdf-problem.is-error", has_text="Undefined control sequence")
    expect(problem).to_be_visible(timeout=60000)
    expect(status).to_have_text(re.compile(r"error"))
    expect(problem.locator(".pdf-problem-where")).to_have_text(f"main.tex:{error_line}")
    page.keyboard.press("Control+Home")
    problem.locator(".pdf-problem-where").click()
    wait_until(lambda: cursor_line(page) == error_line, what="the cursor on the error's line")
    env.shot(page, "pdf-problems-1280")
    ctx.close()

    # On a phone the PDF takes the place of the text, and goes back.
    phone = env.context(browser, "alice", width=390, height=844)
    page = env.open_editor(env.page(phone, "alice"), "main.tex", project=paper)
    expect(page.locator(".editor-pdf")).to_be_hidden()
    page.get_by_role("button", name="PDF", exact=True).click()
    expect(page.locator(".editor-pdf .pdfview-page").first).to_be_visible(timeout=60000)
    expect(page.locator(".editor-main")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    env.shot(page, "pdf-390")
    page.get_by_label("Back to the text").click()
    expect(page.locator(".editor-main")).to_be_visible()
    phone.close()
    return "auto compile, page count, problems, the phone's toggle"


@test
def synctex(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    paper = compile_paper(env, f"SyncTeX {env.run}")
    ctx = env.context(browser, "alice")
    page = env.open_editor(env.page(ctx, "alice"), "main.tex", project=paper)
    expect(page.locator(".pdf-status")).to_have_text(re.compile(r"^Compiled in"), timeout=90000)
    # From the text to the PDF: the cursor's line is marked.
    put_cursor_at_end_of(page, "Forward search lands")
    target = cursor_line(page)
    page.keyboard.press("Control+Shift+Enter")
    mark = page.locator(".pdfview-mark").first
    expect(mark).to_be_visible(timeout=10000)
    env.shot(page, "synctex-1280")
    box = mark.bounding_box()
    # From the PDF to the text: a click there leads back to the line.
    page.keyboard.press("Control+Home")
    page.mouse.click(box["x"] + 20, box["y"] + box["height"] / 2)
    wait_until(lambda: cursor_line(page) in (target, target + 1), what="the cursor back on the paragraph")
    ctx.close()
    return f"line {target} and back"


@test
def versions_pdf(env, browser):
    if not env.compiling:
        return "skipped, needs --compile or --compile-url"
    paper = compile_paper(env, f"Versions {env.run}")
    with env.api("alice") as c:
        v = c.post(f"/api/projects/{paper['id']}/versions", json={"kind": "submission"})
        v.raise_for_status()
        tag = v.json()["tag"]

        def compiled():
            versions = c.get(f"/api/projects/{paper['id']}").json()["versions"]
            done = [x for x in versions if x["tag"] == tag and (x.get("compiled") or {}).get("status") in ("success", "errors")]
            return done[0] if done else None

        wait_until(compiled, timeout=120, interval=1, what="the version's compiled PDF")
    env.put("alice", "main.tex", PAPER.replace("growing concern.", "growing and measurable concern."), project=paper)

    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    page.goto(f"{env.base}/p/{paper['name']}/versions")
    card = page.locator(".release").first
    expect(card.locator(".asset-row", has_text="Compiled PDF")).to_contain_text("Compiled")
    card.get_by_role("button", name="Preview").click()
    expect(card.locator(".pdfview-page")).to_have_count(1, timeout=20000)
    env.shot(page, "versions-pdf-1280")

    page.goto(f"{env.base}/p/{paper['name']}/compare?from={tag}&to=live")
    page.get_by_role("button", name="PDF diff").click()
    expect(page).to_have_url(re.compile(r"pdf=1"))
    expect(page.locator(".pdf-box .pdfview-page").first).to_be_visible(timeout=180000)
    env.shot(page, "pdfdiff-1280")
    ctx.close()


def idle_start(env, browser):
    """Opened first and checked last, so the 90 seconds pass during the other tests."""
    ctx = env.context(browser, "dave")
    page = env.open_editor(env.page(ctx, "dave"), project=env.quiet)
    return ctx, page, time.monotonic()


def idle_check(env, idle):
    ctx, page, since = idle
    left = 90 - (time.monotonic() - since)
    if left > 0:
        page.wait_for_timeout(left * 1000)
    opened = len(page.sockets)
    status = page.locator(".editor-status").inner_text()
    ctx.close()
    assert opened == 1, f"{opened} sockets in 90 s, the connection was cycled"
    assert status == "Connected", status
    return f"one socket for {time.monotonic() - since:.0f} s, still connected"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8065)
    parser.add_argument("--db", default=os.environ.get("MEERTEX_EDITOR_DB", DEFAULT_DB))
    parser.add_argument("--server", help="use this running server instead of starting one (it must use --db)")
    parser.add_argument("--shots", help="save screenshots into this folder")
    parser.add_argument("--tmp", help="the folder for the server's data, logs and the CLI checkout (default: the system's)")
    parser.add_argument("--compile", action="store_true",
                        help="start the compile service natively with this machine's TeX Live. The PDF tests need one")
    parser.add_argument("--compile-port", type=int, default=8075, help="its port with --compile (8075)")
    parser.add_argument("--compile-url", default=os.environ.get("COMPILE_URL"), help="or use this running compile service")
    parser.add_argument("--compile-token", default=os.environ.get("COMPILE_TOKEN"), help="and its token")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-idle", action="store_true", help="skip the 90 second idle test")
    parser.add_argument("only", nargs="*", help="run only the tests with these names")
    args = parser.parse_args()

    env = Env(args)
    failed = 0
    try:
        env.start()
        env.setup()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=not args.headed)
            idle = None if args.no_idle or args.only else idle_start(env, browser)

            def run(tests):
                nonlocal failed
                for fn in tests:
                    if args.only and fn.__name__ not in args.only:
                        continue
                    t0 = time.monotonic()
                    try:
                        detail = fn(env, browser)
                        print(f"ok    {fn.__name__} ({time.monotonic() - t0:.1f} s){f': {detail}' if detail else ''}", flush=True)
                    except Exception:  # noqa: BLE001 (a failed test is reported, the run goes on)
                        failed += 1
                        print(f"FAIL  {fn.__name__}", flush=True)
                        traceback.print_exc()

            run(TESTS)
            if idle:
                try:
                    print(f"ok    idle_editor_stays_connected: {idle_check(env, idle)}", flush=True)
                except Exception:  # noqa: BLE001 (a failed test is reported, the run goes on)
                    failed += 1
                    print("FAIL  idle_editor_stays_connected", flush=True)
                    traceback.print_exc()
            run(LATE)
            browser.close()
        if env.errors:
            print("Errors in the browsers' consoles:")
            for e in env.errors:
                print(f"  {e}")
    finally:
        env.stop()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
