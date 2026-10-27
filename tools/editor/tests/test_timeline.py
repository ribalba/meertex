"""End-to-end tests of the timeline and of planned papers (app/static/js/pages/timeline.js).

Google Chrome through Playwright against a real server and a real database.
tools/editor/README.md says how to run it. Each test is a function below and
they run in order, later ones build on what earlier ones made. A run prints
one line per test and exits 1 when one failed.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

REPO = Path(__file__).resolve().parents[3]
PYTHON = REPO / ".venv" / "bin" / "python"
DEFAULT_DB = "postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_timeline"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# Pixels per day at the timeline's zooms (ZOOMS in timeline.js).
PPD = {"weeks": 28, "months": 6, "quarters": 2}

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def day_text(d) -> str:
    """A day as the web app writes it, "15 Nov 2026"."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


class Env:
    """The server, the people and the papers every test uses."""

    def __init__(self, args):
        self.args = args
        self.port = args.port
        self.base = f"http://127.0.0.1:{self.port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="meertex-timeline-", dir=args.tmp))
        self.server = None
        self.run = secrets.token_hex(3)
        self.shots = Path(args.shots) if args.shots else None
        self.errors = []
        # The day the papers' dates are counted from. AoE is UTC-12, so a
        # date written for the venue is that day there.
        self.today = datetime.now(UTC).date()

    # --- server --------------------------------------------------------------

    def start(self):
        if self.args.server:
            self.base = self.args.server.rstrip("/")
            return
        # Another server on the port would answer the health check below, and
        # the tests would run against its database.
        try:
            httpx.get(f"{self.base}/healthz", timeout=1)
            raise RuntimeError(f"Something listens on port {self.port} already. Pick another with --port.")
        except httpx.HTTPError:
            pass
        env = {
            **os.environ,
            "DATABASE_URL": self.args.db,
            "DATA_DIR": str(self.tmp / "data"),
            "BASE_URL": self.base,
            "SMTP_HOST": "",
            "COMPILE_URL": "",
            "COMPILE_TOKEN": "",
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

    # --- people and papers --------------------------------------------------------

    def make_users(self, people, zone="UTC"):
        """Accounts with a session and an API token, made in the database the
        way a sign-in makes them. Returns {email: (session, token)}. A sign-in
        leaves the zone for the browser to fill in. ``zone`` sets it first, UTC
        by default because day 0 of the axis is today in the reader's zone and
        self.today is counted in UTC. None leaves it to the browser."""
        code = (
            "import sys\n"
            "from app.database import SessionLocal\n"
            "from app.security import create_session, get_or_create_user, new_api_token\n"
            "with SessionLocal() as db:\n"
            "    zone = sys.argv[1] or None\n"
            "    for arg in sys.argv[2:]:\n"
            "        email, name = arg.split('|')\n"
            "        u = get_or_create_user(db, email)\n"
            "        u.name = name\n"
            "        u.timezone = zone\n"
            "        u.api_token = u.api_token or new_api_token()\n"
            "        db.commit()\n"
            "        print(email, create_session(db, u), u.api_token)\n"
        )
        env = {**os.environ, "DATABASE_URL": self.args.db, "DATA_DIR": str(self.tmp / "data")}
        out = subprocess.run([str(PYTHON), "-c", code, zone or "", *[f"{e}|{n}" for e, n in people]],
                             cwd=REPO, env=env, check=True, capture_output=True, text=True).stdout
        return {line.split()[0]: tuple(line.split()[1:]) for line in out.strip().splitlines()}

    def day(self, n):
        return self.today + timedelta(days=n)

    def setup(self):
        r = self.run
        self.people = {
            "alice": (f"alice-{r}@example.com", "Alice Andersson"),
            "bob": (f"bob-{r}@example.com", "Bob Brown"),
        }
        made = self.make_users(self.people.values())
        self.session = {k: made[e][0] for k, (e, _) in self.people.items()}
        self.token = {k: made[e][1] for k, (e, _) in self.people.items()}
        # Two papers whose writing windows overlap. With 30 days to write,
        # alpha writes from day 10 to day 40 and beta from day 20 to day 50.
        self.abstract_day = self.day(20)
        self.alpha = self.create("alice", {
            "title": "Alpha energy study", "conference": "TEST", "year": 2027, "paper": f"alpha{r}",
            "dates": [
                {"kind": "abstract", "due_at": f"{self.abstract_day.isoformat()}T23:59", "tz": "AoE"},
                {"kind": "submission", "date": self.day(40).isoformat(), "tz": "AoE"},
                {"kind": "notification", "date": self.day(100).isoformat(), "tz": "AoE"},
                {"kind": "conference_start", "date": self.day(160).isoformat(), "tz": "Europe/Lisbon"},
                {"kind": "conference_end", "date": self.day(163).isoformat(), "tz": "Europe/Lisbon"},
            ],
        })
        self.beta = self.create("alice", {
            "title": "Beta carbon tracing", "conference": "TEST", "year": 2027, "paper": f"beta{r}",
            "dates": [
                {"kind": "submission", "due_at": f"{self.day(50).isoformat()}T23:59", "tz": "AoE"},
                {"kind": "rebuttal_start", "date": self.day(80).isoformat(), "tz": "AoE"},
                {"kind": "rebuttal_end", "date": self.day(85).isoformat(), "tz": "AoE"},
                {"kind": "notification", "date": self.day(110).isoformat(), "tz": "AoE"},
                {"kind": "camera_ready", "date": self.day(130).isoformat(), "tz": "AoE"},
            ],
        })

    def api(self, who):
        return httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {self.token[who]}"}, timeout=10)

    def create(self, who, body):
        with self.api(who) as c:
            r = c.post("/api/projects", json=body)
            r.raise_for_status()
            return r.json()

    def get(self, who, path):
        with self.api(who) as c:
            r = c.get(path)
            r.raise_for_status()
            return r.json()

    # --- browsers -----------------------------------------------------------------

    def context(self, browser, who, width=1440, height=900, zone=None):
        ctx = browser.new_context(viewport={"width": width, "height": height}, timezone_id=zone)
        ctx.add_cookies([{"name": "meertex_session", "value": self.session[who], "url": self.base}])
        return ctx

    def page(self, ctx, label):
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and self.errors.append(f"{label}: {m.text}"))
        page.on("pageerror", lambda e: self.errors.append(f"{label}: {e}"))
        return page

    def open_timeline(self, page):
        page.goto(f"{self.base}/timeline")
        expect(page.locator(".tl-scroll, .tl-host .blankslate").first).to_be_visible(timeout=10000)
        return page

    def reload_timeline(self, page):
        page.reload()
        expect(page.locator(".tl-scroll, .tl-host .blankslate").first).to_be_visible(timeout=10000)
        return page

    def shot(self, page, name, full=False):
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(self.shots / f"{name}.png"), full_page=full)


# --- helpers ------------------------------------------------------------------------


def writing_bar(page: Page, name: str):
    """The writing bar of a row as {left, width, now}, in pixels on the track."""
    return page.evaluate(
        """(name) => {
            const row = [...document.querySelectorAll('.tl-row')].find((r) => r.dataset.name === name);
            const bar = row?.querySelector('.tl-bar-writing');
            const now = document.querySelector('.tl-axis-now');
            if (!bar || !now) return null;
            return { left: parseFloat(bar.style.left), width: parseFloat(bar.style.width), now: parseFloat(now.style.left) };
        }""",
        name,
    )


def row(page: Page, name: str):
    return page.locator(f'.tl-row[data-name="{name}"]')


def wait_until(fn, timeout=5.0, interval=0.05, what="condition"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout}s waiting for {what} (last: {last!r})")


def no_page_scroll(page: Page):
    """The page itself does not scroll sideways, only the chart may."""
    sizes = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
    assert sizes[0] <= sizes[1], f"the page is {sizes[0]} px wide in a {sizes[1]} px window"


# --- tests -----------------------------------------------------------------------------


@test
def empty_timeline_explains_what_to_do(env, browser):
    ctx = env.context(browser, "bob")
    page = env.open_timeline(env.page(ctx, "bob"))
    empty = page.locator(".tl-host .blankslate")
    expect(empty).to_contain_text("Nothing on the timeline yet")
    expect(empty.locator('a[href="/new"]')).to_be_visible()
    expect(empty.locator('a[href="/new?plan=1"]')).to_be_visible()
    # The tab next to the list leads here and back.
    expect(page.locator('.view-tabs a[aria-current="page"]')).to_have_text("Timeline")
    env.shot(page, "timeline-empty-1440")
    page.locator(".view-tabs a", has_text="List").click()
    expect(page).to_have_url(f"{env.base}/")
    expect(page.locator('.view-tabs a[aria-current="page"]')).to_have_text("List")
    ctx.close()


@test
def hover_shows_the_venues_day(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_timeline(env.page(ctx, "alice"))
    alpha = row(page, env.alpha["name"])
    expect(alpha).to_be_visible()
    env.shot(page, "timeline-months-1440")
    # 23:59 AoE on the abstract day is noon of the next day in Europe and in
    # UTC. The card says the venue's day.
    mark = alpha.locator('.tl-mark[data-kind="abstract"]')
    mark.hover()
    tip = page.locator(".tl-tip")
    expect(tip).to_be_visible()
    when = tip.locator(".tl-tip-when")
    expect(when).to_have_text(f"{day_text(env.abstract_day)}, 23:59 AoE")
    assert day_text(env.abstract_day + timedelta(days=1)) not in when.inner_text()
    expect(tip).to_contain_text("Abstract registration")
    assert tip.locator(".countdown").inner_text().startswith("in "), tip.inner_text()
    env.shot(page, "timeline-hover-1440")
    # A day without a time is shown as the day alone.
    alpha.locator('.tl-mark[data-kind="submission"]').hover()
    expect(when).to_have_text(day_text(env.day(40)))
    # Keyboard: a marker takes focus and shows the same card.
    page.mouse.move(5, 5)
    expect(tip).to_be_hidden()
    mark.focus()
    expect(tip).to_be_visible()
    expect(when).to_have_text(f"{day_text(env.abstract_day)}, 23:59 AoE")
    assert mark.get_attribute("aria-describedby") == tip.get_attribute("id")
    assert day_text(env.abstract_day) in mark.get_attribute("aria-label")
    page.keyboard.press("Escape")
    expect(tip).to_be_hidden()
    # A name in the sticky column takes focus without scrolling the chart.
    scroller = page.locator(".tl-scroll")
    left = scroller.evaluate("(el) => el.scrollLeft")
    assert left > 0, left
    alpha.locator(".tl-name-link").focus()
    assert scroller.evaluate("(el) => el.scrollLeft") == left
    # A marker far to the right is scrolled into sight, clear of the name column.
    far = row(page, env.beta["name"]).locator('.tl-mark[data-kind="camera_ready"]')
    far.focus()
    expect(tip).to_be_visible()
    mark_box, name_box, box = far.bounding_box(), alpha.locator(".tl-name").bounding_box(), scroller.bounding_box()
    assert mark_box["x"] >= name_box["x"] + name_box["width"], (mark_box, name_box)
    assert mark_box["x"] + mark_box["width"] <= box["x"] + box["width"], (mark_box, box)
    ctx.close()


@test
def load_strip_counts_overlapping_windows(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_timeline(env.page(ctx, "alice"))
    two = page.locator('.tl-load-seg[data-count="2"]')
    expect(two).to_have_count(1)
    expect(two).to_have_text("2")
    assert "is-two" in two.get_attribute("class")
    expect(page.locator('.tl-load-seg[data-count="1"]')).to_have_count(2)
    expect(page.locator(".tl-load-seg.is-many")).to_have_count(0)
    two.hover()
    tip = page.locator(".tl-tip")
    expect(tip).to_contain_text("2 writing windows at once")
    expect(tip).to_contain_text(env.alpha["name"])
    expect(tip).to_contain_text(env.beta["name"])
    # Weeks zoom counts per day, and the zoom is remembered.
    page.locator(".segmented button", has_text="Weeks").click()
    expect(page.locator(".tl-canvas.tl-zoom-weeks")).to_be_visible()
    expect(page.locator('.tl-load-seg[data-count="2"]')).to_have_count(1)
    width = float(page.locator('.tl-load-seg[data-count="2"]').evaluate("(el) => parseFloat(el.style.width)"))
    assert 19 * PPD["weeks"] <= width <= 22 * PPD["weeks"], width
    env.shot(page, "timeline-weeks-1440")
    env.reload_timeline(page)
    expect(page.locator(".tl-canvas.tl-zoom-weeks")).to_be_visible()
    page.locator(".segmented button", has_text="Quarters").click()
    expect(page.locator(".tl-canvas.tl-zoom-quarters")).to_be_visible()
    env.shot(page, "timeline-quarters-1440")
    page.locator(".segmented button", has_text="Months").click()
    expect(page.locator(".tl-canvas.tl-zoom-months")).to_be_visible()
    ctx.close()


@test
def writing_days_move_the_bar(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_timeline(env.page(ctx, "alice"))
    name = env.alpha["name"]
    before = writing_bar(page, name)
    assert abs(before["width"] - 30 * PPD["months"]) < 1, before
    days = page.locator(".tl-toolbar .tl-days-input")
    expect(days).to_have_value("30")
    days.fill("45")
    expect(page.locator(".tl-toolbar .tl-saved")).to_have_text("Saved")
    after = writing_bar(page, name)
    shift = (before["left"] - before["now"]) - (after["left"] - after["now"])
    assert abs(shift - 15 * PPD["months"]) < 1, (before, after)
    assert abs(after["width"] - 45 * PPD["months"]) < 1, after
    # The end stays at the hand-in date.
    assert abs((before["left"] + before["width"]) - (after["left"] + after["width"])) < 1, (before, after)
    assert env.get("alice", "/api/auth/me")["writing_days"] == 45
    env.reload_timeline(page)
    expect(page.locator(".tl-toolbar .tl-days-input")).to_have_value("45")
    again = writing_bar(page, name)
    assert abs((again["left"] - again["now"]) - (after["left"] - after["now"])) < 1, (after, again)
    # The paper's overview says when to start, in the venue's day.
    page.goto(f"{env.base}/p/{name}")
    line = page.locator(".tl-writing-line")
    expect(line).to_contain_text(f"{day_text(env.day(40 - 45))}")
    expect(line).to_contain_text("45 days before Paper submission")
    expect(line.locator('a[href="/timeline"]')).to_be_visible()
    env.shot(page, "overview-writing-line-1440")
    # The same field in the settings, back to 30.
    page.goto(f"{env.base}/settings#writing")
    field = page.locator("#writing .tl-days-input")
    expect(field).to_have_value("45")
    field.fill("30")
    expect(page.locator("#writing .tl-saved")).to_have_text("Saved")
    assert env.get("alice", "/api/auth/me")["writing_days"] == 30
    env.shot(page, "settings-writing-1440")
    ctx.close()


@test
def a_paper_keeps_its_own_writing_time(env, browser):
    ctx = env.context(browser, "alice")
    page = env.open_timeline(env.page(ctx, "alice"))
    name = env.beta["name"]
    assert abs(writing_bar(page, name)["width"] - 30 * PPD["months"]) < 1
    row(page, name).locator(".tl-kebab").click()
    pop = page.locator(".tl-pop:popover-open")
    expect(pop).to_be_visible()
    expect(pop).to_contain_text("Writing time for this paper")
    env.shot(page, "timeline-row-menu-1440")
    pop.locator("input").fill("5")
    pop.locator("button[type=submit]").click()
    expect(pop).to_have_count(0)
    wait_until(lambda: abs((writing_bar(page, name) or {}).get("width", 0) - 5 * PPD["months"]) < 1, what="the shorter bar")
    project = env.get("alice", f"/api/projects/{env.beta['id']}")
    assert project["writing_days"] == 5 and project["writing"]["own"] is True, project["writing"]
    # Alpha keeps the usual time.
    assert abs(writing_bar(page, env.alpha["name"])["width"] - 30 * PPD["months"]) < 1
    env.reload_timeline(page)
    assert abs(writing_bar(page, name)["width"] - 5 * PPD["months"]) < 1
    # No overlap left: beta now writes from day 45 to day 50.
    expect(page.locator('.tl-load-seg[data-count="2"]')).to_have_count(0)
    # Empty goes back to the usual time.
    row(page, name).locator(".tl-kebab").click()
    expect(pop.locator("input")).to_have_value("5")
    pop.locator("input").fill("")
    pop.locator("button[type=submit]").click()
    wait_until(lambda: abs((writing_bar(page, name) or {}).get("width", 0) - 30 * PPD["months"]) < 1, what="the usual bar again")
    project = env.get("alice", f"/api/projects/{env.beta['id']}")
    assert project["writing"]["own"] is False, project["writing"]
    ctx.close()


@test
def a_plan_needs_no_title(env, browser):
    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    page.goto(f"{env.base}/new")
    page.locator(".kind-choice .choice", has_text="A plan").click()
    expect(page).to_have_url(f"{env.base}/new?plan=1")
    expect(page.locator("h1")).to_have_text("Plan a paper for a venue")
    # The short name and the starter are not asked for a plan.
    expect(page.get_by_label("Short name")).to_be_hidden()
    expect(page.locator("legend", has_text="Start with")).to_be_hidden()
    page.get_by_label("Conference", exact=True).fill("ICT4S")
    page.get_by_label("Year", exact=True).fill("2027")
    expect(page.locator(".name-preview .name")).to_have_text("2027-ict4s")
    rows = page.locator(".date-row")
    rows.first.locator(".date-remove").click()
    expect(rows).to_have_count(1)
    rows.first.locator("input[type=date]").fill(env.day(120).isoformat())
    env.shot(page, "new-plan-1440", full=True)
    page.locator("button[type=submit]", has_text="Create plan").click()
    expect(page).to_have_url(f"{env.base}/p/2027-ict4s")
    box = page.locator(".tl-plan-box")
    expect(box).to_contain_text("This is a plan for ICT4S 2027. Start the paper when the topic is clear.")
    expect(page.locator(".setup-box")).to_have_count(0)
    env.shot(page, "overview-plan-1440")
    plan = env.get("alice", "/api/projects/2027-ict4s")
    assert plan["planned"] is True and not plan["title"], plan
    env.plan = plan

    # The list marks it and filters by it.
    page.goto(f"{env.base}/")
    plan_row = page.locator(".project-row", has=page.locator(".project-name", has_text="2027-ict4s"))
    expect(plan_row.locator(".label-planned")).to_have_text("Planned")
    expect(page.locator(".project-row .label-planned")).to_have_count(1)
    env.shot(page, "list-planned-1440")
    page.goto(f"{env.base}/?planned=true")
    expect(page.locator(".project-row")).to_have_count(1)
    expect(page.locator(".filter-bar")).to_contain_text("Planned: Only plans")
    page.goto(f"{env.base}/?planned=false")
    expect(page.locator(".project-row")).to_have_count(2)
    expect(page.locator(".project-row .label-planned")).to_have_count(0)

    # The timeline draws it dashed, with its badge.
    env.open_timeline(page)
    plan_tl = row(page, "2027-ict4s")
    expect(plan_tl).to_have_class("tl-row is-planned")
    expect(plan_tl.locator(".label-planned")).to_be_visible()
    style = plan_tl.locator(".tl-bar-writing").evaluate("(el) => getComputedStyle(el).borderTopStyle")
    assert style == "dashed", style
    env.shot(page, "timeline-plan-1440")
    ctx.close()


@test
def starting_a_plan_makes_a_paper(env, browser):
    ctx = env.context(browser, "alice")
    page = env.page(ctx, "alice")
    page.goto(f"{env.base}/p/2027-ict4s")
    page.locator(".tl-plan-box button", has_text="Start the paper").click()
    dialog = page.locator("dialog.dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_label("Title").fill("Carbon aware builds")
    expect(dialog.locator(".name-preview .name")).to_have_text("2027-ict4s-carbon-aware-builds")
    dialog.locator(".choice", has_text="Basic").click()
    env.shot(page, "start-dialog-1440")
    dialog.locator(".dialog-footer button", has_text="Start the paper").click()
    expect(page).to_have_url(f"{env.base}/p/2027-ict4s-carbon-aware-builds")
    expect(page.locator(".repo-subtitle")).to_have_text("Carbon aware builds")
    expect(page.locator(".tl-plan-box")).to_have_count(0)
    project = env.get("alice", f"/api/projects/{env.plan['id']}")
    assert project["planned"] is False and project["title"] == "Carbon aware builds", project
    assert project["name"] == "2027-ict4s-carbon-aware-builds", project["name"]
    assert project["file_count"] == 2, project["file_count"]
    env.open_timeline(page)
    started = row(page, "2027-ict4s-carbon-aware-builds")
    expect(started).to_have_class("tl-row")
    expect(started).to_contain_text("Carbon aware builds")
    ctx.close()


@test
def phone_width_scrolls_the_chart_only(env, browser):
    ctx = env.context(browser, "alice", width=390, height=844)
    page = env.open_timeline(env.page(ctx, "alice"))
    no_page_scroll(page)
    scroller = page.locator(".tl-scroll")
    sizes = scroller.evaluate("(el) => [el.scrollWidth, el.clientWidth]")
    assert sizes[0] > sizes[1], sizes
    env.shot(page, "timeline-390", full=True)
    name_cell = row(page, env.alpha["name"]).locator(".tl-name")
    x0 = name_cell.bounding_box()["x"]
    scroller.evaluate("(el) => { el.scrollLeft += 300; }")
    page.wait_for_timeout(50)
    assert abs(name_cell.bounding_box()["x"] - x0) < 1, "the name column moved"
    no_page_scroll(page)
    # The row menu and a hover card stay inside the window.
    row(page, env.beta["name"]).locator(".tl-kebab").click()
    pop = page.locator(".tl-pop:popover-open").bounding_box()
    assert pop["x"] >= 0 and pop["x"] + pop["width"] <= 390, pop
    env.shot(page, "timeline-menu-390")
    page.keyboard.press("Escape")
    for path, shot in [("/", "list-390"), ("/new?plan=1", "new-plan-390"), (f"/p/{env.alpha['name']}", "overview-390")]:
        page.goto(env.base + path)
        page.wait_for_load_state("networkidle")
        no_page_scroll(page)
        env.shot(page, shot, full=True)
    ctx.close()


@test
def finished_papers_and_folders(env, browser):
    # A rejected paper in a folder of its own. Finished papers are hidden at first.
    gamma = env.create("alice", {
        "title": "Gamma rejected", "conference": "OLD", "year": 2026, "paper": f"gamma{env.run}",
        "folder": "Archive/2026",
        "dates": [{"kind": "submission", "date": env.day(-60).isoformat(), "tz": "AoE"},
                  {"kind": "notification", "date": env.day(-10).isoformat(), "tz": "AoE"}],
    })
    with env.api("alice") as c:
        c.patch(f"/api/projects/{gamma['id']}", json={"outcome": "rejected"}).raise_for_status()
    ctx = env.context(browser, "alice")
    page = env.open_timeline(env.page(ctx, "alice"))
    expect(row(page, gamma["name"])).to_have_count(0)
    finished = page.get_by_label("Show finished papers")
    expect(page.locator(".tl-check")).to_contain_text("(1)")
    finished.check()
    expect(row(page, gamma["name"])).to_have_class("tl-row is-finished")
    # Remembered in this browser.
    env.reload_timeline(page)
    expect(page.get_by_label("Show finished papers")).to_be_checked()
    expect(row(page, gamma["name"])).to_be_visible()
    env.shot(page, "timeline-finished-1440")
    # The folder filter, subfolders included.
    page.locator(".tl-toolbar summary", has_text="Folder").click()
    page.locator(".dropdown-menu .dropdown-item", has_text="Archive").first.click()
    expect(page.locator(".tl-row")).to_have_count(1)
    expect(row(page, gamma["name"])).to_be_visible()
    env.reload_timeline(page)
    expect(page.locator(".tl-row")).to_have_count(1)
    # Without finished papers the folder is empty, and the page says why.
    page.get_by_label("Show finished papers").uncheck()
    expect(page.locator(".tl-host .blankslate")).to_contain_text("1 finished paper is hidden")
    page.locator(".tl-toolbar summary", has_text="Folder").click()
    page.locator(".dropdown-menu .dropdown-item", has_text="All folders").click()
    expect(row(page, env.alpha["name"])).to_be_visible()
    expect(row(page, gamma["name"])).to_have_count(0)
    ctx.close()


@test
def a_shared_link_shows_the_chart_only(env, browser):
    # A paper in a folder of its own, for a link that shows only that folder.
    delta = env.create("alice", {
        "title": "Delta for the group", "conference": "GROUP", "year": 2027, "paper": f"delta{env.run}",
        "folder": "Supervised/2027", "dates": [{"kind": "submission", "date": env.day(70).isoformat(), "tz": "AoE"}],
    })
    ctx = env.context(browser, "alice")
    page = env.open_timeline(env.page(ctx, "alice"))
    share = page.locator(".tl-toolbar button", has_text="Share")
    expect(share).to_have_text("Share")
    share.click()
    dialog = page.locator("dialog.dialog", has_text="Share the timeline")
    expect(dialog).to_contain_text("Anyone with the link sees this chart without signing in")
    expect(dialog.get_by_label("Papers", exact=True)).to_have_value("")
    env.shot(page, "share-dialog-new-1440")
    dialog.locator(".dialog-footer button", has_text="Create link").click()
    link = dialog.get_by_label("Link", exact=True)
    expect(link).to_have_value(re.compile(r"/timeline/[A-Za-z0-9_-]{40,}$"))
    url = link.input_value()
    assert url.startswith(env.base), url
    expect(share).to_have_text("Shared")
    expect(dialog.locator('a[target="_blank"]', has_text="Open it")).to_have_attribute("href", url)
    env.shot(page, "share-dialog-1440")
    dialog.locator(".dialog-footer button", has_text="Done").click()
    expect(dialog).to_have_count(0)

    # Someone without an account sees the chart and nothing else of meertex.
    guest = browser.new_context(viewport={"width": 1440, "height": 900})
    gp = env.page(guest, "guest")
    gp.goto(url)
    expect(gp.locator(".tl-scroll")).to_be_visible(timeout=10000)
    expect(gp.locator("h1")).to_have_text("Alice Andersson's timeline")
    alpha = row(gp, env.alpha["name"])
    expect(alpha).to_contain_text("Alpha energy study")
    expect(row(gp, delta["name"])).to_be_visible()
    for hidden in (".tl-row a", ".tl-kebab", ".tl-days-input", ".tl-toolbar .dropdown", ".view-tabs", "#header-actions > *"):
        expect(gp.locator(hidden)).to_have_count(0)
    alpha.locator(".tl-bar-writing").hover()
    expect(gp.locator(".tl-tip")).to_contain_text("the usual time")
    gp.locator(".segmented button", has_text="Weeks").click()
    expect(gp.locator(".tl-canvas.tl-zoom-weeks")).to_be_visible()
    gp.locator(".segmented button", has_text="Months").click()
    assert gp.url == url, gp.url
    env.shot(gp, "shared-1440")
    phone = browser.new_context(viewport={"width": 390, "height": 844})
    pp = env.page(phone, "guest-phone")
    pp.goto(url)
    expect(pp.locator(".tl-scroll")).to_be_visible(timeout=10000)
    no_page_scroll(pp)
    env.shot(pp, "shared-390", full=True)
    phone.close()

    # One folder, subfolders included. The link stays the same.
    share.click()
    dialog.get_by_label("Papers", exact=True).select_option(label="Supervised")
    expect(dialog.locator(".tl-saved")).to_have_text("Saved")
    expect(dialog.get_by_label("Link", exact=True)).to_have_value(url)
    dialog.locator(".dialog-footer button", has_text="Done").click()
    gp.reload()
    expect(gp.locator(".tl-row")).to_have_count(1)
    expect(row(gp, delta["name"])).to_be_visible()

    # A new link ends the old one, and stopping ends the new one.
    share.click()
    dialog.locator(".dialog-footer button", has_text="New link").click()
    page.locator("dialog.dialog", has_text="Make a new link?").locator("button", has_text="Make a new link").click()
    expect(dialog.get_by_label("Link", exact=True)).not_to_have_value(url)
    expect(dialog.get_by_label("Papers", exact=True).locator("option:checked")).to_have_text("Supervised")
    renewed = dialog.get_by_label("Link", exact=True).input_value()
    dialog.locator(".dialog-footer button", has_text="Stop sharing").click()
    page.locator("dialog.dialog", has_text="Stop sharing the timeline?").locator("button", has_text="Stop sharing").click()
    expect(dialog.locator(".dialog-footer button", has_text="Create link")).to_be_visible()
    expect(share).to_have_text("Share")
    assert env.get("alice", "/api/timeline/share") is None
    # A dead link's 404 is expected here, so this page does not report console errors.
    dead = guest.new_page()
    for gone in (url, renewed):
        dead.goto(gone)
        expect(dead.locator(".blankslate")).to_contain_text("This link does not work")
    env.shot(dead, "shared-gone-1440")
    guest.close()
    ctx.close()


@test
def a_new_account_takes_the_browsers_zone(env, browser):
    email = f"carol-{env.run}@example.com"
    env.session["carol"], env.token["carol"] = env.make_users([(email, "Carol Chen")], zone=None)[email]
    assert env.get("carol", "/api/auth/me")["timezone"] is None
    # The first browser's zone stays. One in another zone changes nothing.
    for zone in ("Asia/Tokyo", "America/New_York"):
        ctx = env.context(browser, "carol", zone=zone)
        page = env.page(ctx, "carol")
        page.goto(f"{env.base}/settings")
        expect(page.get_by_label("Time zone", exact=True)).to_have_value("Asia/Tokyo")
        ctx.close()
    assert env.get("carol", "/api/auth/me")["timezone"] == "Asia/Tokyo"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8069)
    parser.add_argument("--db", default=os.environ.get("MEERTEX_TIMELINE_DB", DEFAULT_DB))
    parser.add_argument("--server", help="use this running server instead of starting one (it must use --db)")
    parser.add_argument("--shots", help="save screenshots at 1440 and 390 pixels into this folder")
    parser.add_argument("--tmp", help="the folder for the server's data and log (default: the system's)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder with the server's log")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("only", nargs="*", help="run only the tests with these names")
    args = parser.parse_args()

    env = Env(args)
    failed = 0
    try:
        env.start()
        env.setup()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=not args.headed)
            for fn in TESTS:
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
            browser.close()
        if env.errors:
            failed += 1
            print("Errors in the browsers' consoles:")
            for e in env.errors:
                print(f"  {e}")
    finally:
        env.stop()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
