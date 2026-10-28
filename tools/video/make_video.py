#!/usr/bin/env python3
"""Films the narrated tour of meertex for the landing page (tools/video/README.md).

Everything in it is the real app: the server, the compile service and the demo
account of the screenshots (tools/website/shots.py), a real `meertex sync` of
Lena's checkout, Google Chrome on virtual screens and bash in a pseudo terminal
(stage.py). Kokoro speaks the narration (kokoro_say.py), and ffmpeg puts the
clips together with the voice, burned-in subtitles, a quiet pad and the end
card. publish.py turns the result into the files of the landing page.

Every run starts from an empty database, the database of its own
meertex_video_website, so it can run again at any time.
"""

import argparse
import array
import json
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from playwright.sync_api import expect, sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tools" / "website"))

import shots  # noqa: E402  the demo account and the server of the screenshots
import stage  # noqa: E402
from stage import CSS_H, CSS_W, FPS, OUT_H, OUT_W, REC_H, REC_W, Camera  # noqa: E402

MEERTEX = REPO / ".venv" / "bin" / "meertex"
STATIC = REPO / "app" / "static"
DEFAULT_DB = "postgresql+psycopg://meertex:meertex@127.0.0.1:5436/meertex_video_website"

# The narration voice, the same as meerora's demo video. narration.txt has the
# pronunciation lexicon that Kokoro speaks as phonemes.
KOKORO_VOICE = "af_heart"
KOKORO_SPEED = 0.95
KOKORO_MODELS = [Path.home() / ".cache" / "kokoro", Path.home() / ".cache" / "meerora" / "voice" / "kokoro"]

GAP = 1.3  # seconds between two sentences in the same shot
SHOT_LEAD = 0.5  # seconds before the first sentence of a shot that follows a cut
SHOT_TAIL = 1.0  # seconds after the last sentence of a shot before a cut
XFADE = 0.5  # seconds of crossfade between two scenes
PREROLL = 0.8  # seconds the recorders run before the action starts

# The terminal windows, in recorded pixels, each on a screen of this size: a
# narrow one for vim that leaves the editor's text free, and a wide one for the
# command line tool.
TERMINALS = {"vim": (940, 780), "cli": (1200, 800)}
TERM_RADIUS = 18
SHADOW_PAD = 60

PAPER = "2027-eurosys-green-llm"
INTRO = "sections/introduction.tex"


# --- the story ----------------------------------------------------------------------
#
# Each scene is one [section] of narration.txt. Each shot is filmed in one take:
# prepare() sets the page up before the recorders start, act() plays while they
# run, timed by the cue (the shot's sentences in seconds from its start).


class Cue:
    def __init__(self, t0: float, says: list[float], length: float):
        self.t0, self.say, self.length = t0, says, length

    def now(self) -> float:
        return time.monotonic() - self.t0

    def at(self, t: float):
        wait = self.t0 + t - time.monotonic()
        if wait > 0:
            time.sleep(wait)


@dataclass
class Shot:
    id: str
    who: str = "lena"  # who is signed in in the browser
    says: int = 0  # how many sentences of the scene are spoken during this shot, in order
    hold: float = 4.5  # the shortest this shot may be, in seconds
    prepare: Callable | None = None
    act: Callable | None = None
    camera: Callable[[Cue], Camera] | None = None
    # The terminal over the browser (a key of TERMINALS) and where its window
    # sits, in recorded pixels.
    terminal: tuple[str, tuple[int, int]] | None = None
    # Where the subtitles sit during this shot: ASS alignment, MarginL, MarginR,
    # MarginV. None keeps them at the bottom in the middle.
    subs: tuple[int, int, int, int] | None = None
    still: float = 0.5  # when in the shot its still is taken, as a share of its length
    frames: int = 0  # filled by plan()
    start: float = 0.0  # in its scene, filled by plan()
    cues: list[float] = field(default_factory=list)  # filled by plan()


@dataclass
class Scene:
    id: str
    shots: list[Shot]
    lead: float = 0.7  # seconds before the first sentence
    tail: float = 1.5  # seconds after the last sentence
    subtitles: bool = True
    sentences: list[tuple[str, Path, float]] = field(default_factory=list)  # filled by speak()
    offsets: list[float] = field(default_factory=list)  # filled by plan()
    frames: int = 0


def settle(page, ms: int = 500):
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(ms)


def open_editor(st, path: str = INTRO, compiled: bool = True, page=None):
    page = page or st.page
    page.goto(st.run.url("green_llm", f"/edit/{path}"))
    expect(page.locator(".editor-status")).to_have_text("Connected", timeout=30000)
    if compiled:
        expect(page.locator(".pdf-status")).to_have_text(re.compile(r"^Compiled in"), timeout=180000)
        expect(page.locator(".pdfview-page").first).to_be_visible()
    settle(page, 800)


def on_editor(st, path: str = INTRO) -> bool:
    return st.page.url.endswith(f"/edit/{path}") and st.page.locator(".editor-status").text_content() == "Connected"


def caret_line(page, needle: str) -> int:
    """Put the editor's cursor at the end of the first line holding needle."""
    line = shots.put_cursor(page, needle)
    page.evaluate("window.scrollTo(0, 0)")
    return line


def scroll_editor(page, line: int):
    """Scroll the editor so that line is the first one in view."""
    page.evaluate(
        """async (n) => {
            const { EditorView } = await import('/static/vendor/editor/editor.js');
            const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
            view.dispatch({ effects: EditorView.scrollIntoView(view.state.doc.line(n).from, { y: 'start' }) });
        }""",
        line,
    )
    # CodeMirror scrolls the window too, a frame later. Put the page back.
    settle(page, 400)
    page.evaluate("window.scrollTo(0, 0)")
    settle(page, 300)


# 1. intro: the paper list.


def home_prepare(st):
    st.page.goto(f"{st.run.base}/")
    expect(st.page.locator(".next-date-when").first).to_be_visible(timeout=20000)
    expect(st.page.get_by_text("Did you send a rebuttal?").first).to_be_visible()
    settle(st.page)
    st.window.mouse = (1180, 700)
    st.window.park()


def countdown_of(page, name: str):
    """The countdown in a paper's row of the list. Its words depend on the time
    of day, because the deadlines are dates in AoE ("in 3 days" before 13:00 in
    Berlin, "in 2 days" after)."""
    row = page.locator(".project-row", has=page.get_by_role("link", name=name, exact=True))
    return row.locator(".next-date-when").first


def home_act(st, cue):
    page = st.page
    cue.at(cue.say[0] + 1.6)
    st.window.point(page.get_by_role("link", name=PAPER).first, 1.2)
    cue.at(cue.say[1] + 0.2)
    st.window.point(countdown_of(page, PAPER), 1.0)
    cue.at(cue.say[1] + 2.4)
    st.window.point(countdown_of(page, "2027-e-energy-wasm-energy"), 0.9)


def home_camera(cue):
    return Camera([(0, 1.0, (768, 432)), (cue.length, 1.14, (800, 470))])


# 2. local: Lena writes in vim, Tomás sees it in the browser.

LOCAL_LINE = "A single GPU serves hundreds of requests from many users at the same time."


def local_prepare(st):
    open_editor(st)
    st.page.evaluate("window.scrollTo(0, 0)")
    t = st.terminals["vim"]
    t.set_title(f"~/papers/{PAPER}")
    t.run(f"cd ~/papers/{PAPER} && clear", r"green-llm\S* \$ ")
    since = t.mark()
    t.send(f"vim +6 {INTRO}\r")
    t.wait_for(r"Production deployments", since)
    time.sleep(1.0)


def local_act(st, cue):
    t = st.terminals["vim"]
    cue.at(cue.say[0] + 0.5)
    t.send("o")
    time.sleep(0.3)
    t.type_for(LOCAL_LINE, max(2.5, cue.say[1] - cue.now() - 1.1))
    time.sleep(0.25)
    t.send("\x1b")
    cue.at(cue.say[1] - 0.6)
    t.type(":w", cps=7)
    time.sleep(0.2)
    t.send("\r")
    # The line arrives in Tomás's browser through meertex sync and the server.
    wait = time.monotonic()
    expect(st.page.locator(".cm-content")).to_contain_text("A single GPU serves hundreds", timeout=10000)
    st.notes.append(f"local: the save reached the browser in {time.monotonic() - wait:.2f} s")
    cue.at(cue.length - 0.3)
    t.send(":q\r")


def local_camera(cue):
    whole = (768, 432)
    return Camera([(0, 1.0, whole), (cue.say[1] + 0.2, 1.0, whole), (cue.say[1] + 1.6, 1.15, (900, 430))])


# 3. browser: Tomás types in the same file, Lena cites and the PDF compiles.

TOMAS_LINE = "\\item An open-source tool that runs next to any serving system."


def coauthor_prepare(st):
    if not on_editor(st):
        open_editor(st)
    tomas = st.other("tomas")
    open_editor(st, page=tomas, compiled=False)
    shots.put_cursor(tomas, "\\item A public data set")
    caret_line(st.page, "how much energy that request used.")
    # Tomás's new line must be in view without scrolling.
    scroll_editor(st.page, 9)
    st.window.mouse = (700, 560)
    st.window.park()


def coauthor_act(st, cue):
    tomas = st.other("tomas")
    cue.at(cue.say[0] + 0.2)
    tomas.keyboard.press("Enter")
    tomas.keyboard.type(TOMAS_LINE, delay=62)
    time.sleep(0.4)
    caret = st.page.locator(".cm-ySelectionCaret").first
    st.window.point(caret, 0.8, dx=0.5, dy=0.6)
    time.sleep(0.1)
    caret.hover()


def coauthor_camera(cue):
    return Camera([(0, 1.25, (560, 560)), (cue.length, 1.3, (560, 575))])


def cite_prepare(st):
    if not on_editor(st):
        open_editor(st)
    caret_line(st.page, "how much energy that request used.")
    scroll_editor(st.page, 1)
    st.window.mouse = (1240, 640)
    st.window.park()


def cite_act(st, cue):
    page = st.page
    cue.at(cue.say[0] - 0.3)
    page.keyboard.type(" Pricing inference by energy needs the same figures~\\cite{", delay=42)
    expect(page.locator(".cm-tooltip-autocomplete")).to_be_visible(timeout=5000)
    time.sleep(1.1)
    # The completion puts in the key. The brace and the full stop are typed.
    page.keyboard.press("Enter")
    time.sleep(0.3)
    page.keyboard.type("}.", delay=90)
    st.marks["cite_typed"] = cue.now()
    expect(page.locator(".pdf-status")).to_have_text(re.compile(r"^Compil(ing|ed)"), timeout=8000)


def cite_camera(cue):
    near, whole = (560, 330), (768, 432)
    typed = cue.say[0] + 4.4
    return Camera([(0, 1.22, near), (typed, 1.22, near), (typed + 1.4, 1.0, whole)])


# 4. refs: the reference check of the ESEM paper finds the wrong year and the
# made-up paper (demo/refs.bib).

MADE_UP = "pereira2024labels"


def refs_prepare(st):
    page = st.page
    page.goto(st.run.url("metrics_esem", "/references"))
    expect(page.get_by_role("button", name="Check references")).to_be_enabled(timeout=20000)
    settle(page, 600)
    st.window.mouse = (1100, 600)
    st.window.park()


def refs_act(st, cue):
    page = st.page
    cue.at(cue.say[0] + 0.8)
    st.window.click(page.get_by_role("button", name="Check references"), 1.0)
    expect(page.locator(f"#ref-{MADE_UP} .rf-status")).to_contain_text("Error", timeout=60000)
    st.notes.append(f"refs: the check on camera took {cue.now() - cue.say[0] - 1.9:.1f} s")
    cue.at(cue.say[1] - 0.3)
    st.window.click(page.locator(f"#ref-{MADE_UP} .rf-toggle"), 0.9)
    time.sleep(0.3)
    # The row's findings are below the fold. Bring the row up.
    page.evaluate("""(key) => {
        const row = document.getElementById('ref-' + key);
        window.scrollTo({ top: row.getBoundingClientRect().top + window.scrollY - 150, behavior: 'smooth' });
    }""", MADE_UP)
    st.window.glide(st.window.mouse[0] + 60, st.window.mouse[1] - 260, 1.0)


def refs_camera(cue):
    top = (768, 400)
    return Camera([(0, 1.1, top), (cue.say[1] + 0.2, 1.1, top), (cue.say[1] + 1.2, 1.2, (768, 400))])


# 5. venue: the overview, the question and the version it makes.


def overview_prepare(st):
    st.page.goto(st.run.url("carbon_ci"))
    expect(st.page.get_by_text("Did you send a rebuttal?").first).to_be_visible(timeout=20000)
    settle(st.page)
    st.window.mouse = (900, 700)
    st.window.park()


def overview_act(st, cue):
    page = st.page
    cue.at(cue.say[0] + 0.8)
    st.window.point(page.get_by_text(re.compile(r"moved from")).first, 1.1)
    cue.at(cue.say[0] + 3.0)
    st.window.point(page.get_by_text("Example City").first, 1.0)


def overview_camera(cue):
    return Camera([(0, 1.12, (700, 560)), (cue.length, 1.2, (760, 600))])


def question_prepare(st):
    if "/p/" not in st.page.url or st.page.get_by_text("Did you send a rebuttal?").count() == 0:
        overview_prepare(st)
    st.window.park()


def question_act(st, cue):
    page = st.page
    banner = page.locator(".question-actions").first
    cue.at(cue.say[0] + 0.6)
    st.window.point(banner.get_by_role("button", name="Yes"), 1.0)
    cue.at(cue.say[1] - 0.5)
    st.window.click(banner.get_by_role("button", name="Yes"), 0.3)
    dialog = page.locator("dialog[open]")
    expect(dialog).to_be_visible(timeout=5000)
    cue.at(cue.say[1] + 1.6)
    st.window.point(dialog.get_by_text("The state at the deadline"), 0.8)
    cue.at(cue.say[1] + 3.0)
    st.window.click(dialog.get_by_role("button", name="Save version"), 0.8)
    expect(dialog).to_be_hidden(timeout=10000)


def question_camera(cue):
    banner, dialog = (540, 250), (768, 400)
    return Camera([(0, 1.3, banner), (cue.say[1], 1.3, banner), (cue.say[1] + 0.8, 1.15, dialog)])


def versions_prepare(st):
    if "/p/" not in st.page.url:
        overview_prepare(st)
    st.wait_for_version("carbon_ci", "rebuttal")
    st.window.park()


def versions_act(st, cue):
    page = st.page
    cue.at(0.3)
    st.window.click(page.get_by_role("link", name=re.compile(r"^Versions")).first, 0.7)
    card = page.locator(".release").first
    expect(card).to_be_visible(timeout=10000)
    preview = card.get_by_role("button", name="Preview")
    cue.at(1.8)
    if preview.count():
        st.window.click(preview, 0.7)
        expect(card.locator(".pdfview-page").first).to_be_visible(timeout=30000)


def versions_camera(cue):
    return Camera([(0, 1.05, (768, 432)), (cue.length, 1.12, (700, 470))])


# 6. compare: the PDF diff of the journal paper.


def pdfdiff_prepare(st):
    page = st.page
    page.goto(st.run.url("energy_repro", "/compare?from=submission&to=live&pdf=1"))
    expect(page.locator(".pdf-box .pdfview-page").first).to_be_visible(timeout=240000)
    settle(page, 1200)
    page.evaluate("""() => {
        const first = document.querySelector('.pdf-box .pdfview-page');
        first.closest('.pdfview').scrollTop = first.offsetTop + first.offsetHeight * 0.12;
    }""")
    settle(page, 1200)
    st.window.mouse = (1100, 640)
    st.window.park()


def pdfdiff_act(st, cue):
    cue.at(cue.say[0] + 0.4)
    st.page.evaluate("""(seconds) => new Promise((done) => {
        const view = document.querySelector('.pdf-box .pdfview');
        const from = view.scrollTop, by = view.clientHeight * 0.45, t0 = performance.now();
        const step = (now) => {
            const t = Math.min(1, (now - t0) / (seconds * 1000));
            view.scrollTop = from + by * (t * t * (3 - 2 * t));
            if (t < 1) requestAnimationFrame(step); else done();
        };
        requestAnimationFrame(step);
    })""", 3.2)


def pdfdiff_camera(cue):
    return Camera([(0, 1.1, (768, 470)), (cue.length, 1.18, (768, 480))])


# 7. review: Priya selects words in the PDF and comments on them in the source,
# then accepts how a comment was handled.

# Words on one line of the PDF, from line 7 of the introduction.
REVIEW_PICK = "the power sensor reports one number"
REVIEW_COMMENT = "Which sensor is this? Please name it and its sampling rate."


def pdf_words(page, words: str) -> dict:
    """Where words are in the PDF's text layer: the middle of the left edge of
    their first letter (x0, y0) and of the right edge of their last (x1, y1)."""
    at = page.evaluate(
        """(words) => {
            const layer = document.querySelector('.pdfview');
            const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT, {
                acceptNode: (n) => n.parentElement.closest('.textLayer') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
            });
            // Every letter but the spaces with its node, because the layer splits
            // the text into spans where it likes.
            const chars = [];
            let text = '';
            for (let node; (node = walker.nextNode());) {
                for (let i = 0; i < node.data.length; i++) {
                    if (/\\s/.test(node.data[i])) continue;
                    chars.push([node, i]);
                    text += node.data[i];
                }
            }
            const want = words.replace(/\\s+/g, '');
            const from = text.indexOf(want);
            if (from < 0) return null;
            const range = document.createRange();
            range.setStart(...chars[from]);
            const [node, i] = chars[from + want.length - 1];
            range.setEnd(node, i + 1);
            const rects = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
            const first = rects[0], last = rects[rects.length - 1];
            return { x0: first.left, y0: first.top + first.height / 2, x1: last.right, y1: last.top + last.height / 2 };
        }""",
        words,
    )
    if not at:
        raise RuntimeError(f"the PDF's text layer has no {words!r}")
    return at


def review_files_prepare(st):
    page = st.page
    page.goto(st.run.url("green_llm", f"/reviews/{st.run.review['id']}/files?path={INTRO}"))
    expect(page.locator(".rv-thread").first).to_be_visible(timeout=20000)
    expect(page.locator(".pdfview-page").first).to_be_visible(timeout=180000)
    expect(page.locator(".pdfview .textLayer span").first).to_be_attached(timeout=20000)
    settle(page, 1000)
    pdf_words(page, REVIEW_PICK)
    # Finding the words in the source scrolls the window to its end as well, so
    # the shot starts there and the page does not jump.
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    settle(page, 400)
    st.window.mouse = (520, 560)
    st.window.park()


def review_files_act(st, cue):
    page = st.page
    cue.at(cue.say[0] + 0.5)
    st.window.point(page.locator(".rv-thread").first, 1.2, dy=0.3)
    cue.at(cue.say[1] - 0.8)
    # A drag over the words, as a reader selects them. They are marked in the
    # source, and the Comment button under them opens the box there.
    at = pdf_words(page, REVIEW_PICK)
    st.window.glide(at["x0"] + 1, at["y0"], 0.9)
    time.sleep(0.15)
    page.mouse.down()
    st.window.glide(at["x1"] - 1, at["y1"], 1.1)
    time.sleep(0.1)
    page.mouse.up()
    button = page.locator(".rv-pdf-float")
    expect(button).to_be_visible(timeout=10000)
    expect(page.locator(".rv-pick").first).to_be_visible()
    time.sleep(0.7)
    st.window.click(button, 0.6)
    compose = page.locator(".rv-compose")
    expect(compose).to_be_visible(timeout=10000)
    time.sleep(0.5)
    compose.locator("textarea").first.press_sequentially(REVIEW_COMMENT, delay=40)
    time.sleep(0.4)
    st.window.click(compose.get_by_role("button", name="Add single comment"), 0.7)
    expect(compose).to_be_hidden(timeout=10000)


def review_files_camera(cue):
    return Camera([(0, 1.0, (768, 432)), (cue.say[1] + 1.4, 1.0, (768, 432)), (cue.say[1] + 3.0, 1.1, (700, 450))])


def review_prepare(st):
    page = st.page
    page.goto(st.run.url("green_llm", f"/reviews/{st.run.review['id']}"))
    expect(page.locator(".rv-thread").first).to_be_visible(timeout=20000)
    settle(page, 800)
    # The resolved caption with how it was changed, in the middle of the view.
    page.evaluate("""() => {
        const how = [...document.querySelectorAll('.rv-thread')].find((t) => t.textContent.includes('How it was changed'));
        window.scrollTo(0, how.getBoundingClientRect().top + window.scrollY - 90);
    }""")
    settle(page, 600)
    st.window.mouse = (1150, 640)
    st.window.park()


def review_act(st, cue):
    page = st.page
    thread = page.locator(".rv-thread", has_text="How it was changed").first
    cue.at(cue.say[0] + 0.4)
    st.window.point(thread.get_by_text("How it was changed").first, 1.0)
    cue.at(cue.say[0] + 2.6)
    accept = thread.get_by_role("button", name="Accept")
    if accept.count():
        st.window.click(accept.first, 0.9)
        # An accepted thread folds up. Keep it in view with its new label.
        time.sleep(0.6)
        page.evaluate("""() => {
            const t = [...document.querySelectorAll('.rv-thread')].find((t) =>
                t.textContent.includes('sections/evaluation.tex') && t.textContent.includes('Accepted'));
            if (t) window.scrollTo({ top: t.getBoundingClientRect().top + window.scrollY - 200, behavior: 'smooth' });
        }""")


def review_camera(cue):
    return Camera([(0, 1.18, (700, 440)), (cue.length, 1.24, (720, 460))])


# 8. timeline.


def timeline_prepare(st):
    page = st.page
    page.goto(f"{st.run.base}/timeline")
    page.wait_for_load_state("networkidle")
    settle(page, 1200)
    st.window.mouse = (1000, 620)
    st.window.park()


def timeline_camera(cue):
    return Camera([(0, 1.06, (740, 440)), (cue.length, 1.14, (800, 470))])


# 9. agents: the command line tool in the terminal.


def agents_prepare(st):
    st.page.goto(f"{st.run.base}/")
    expect(st.page.locator(".next-date-when").first).to_be_visible(timeout=20000)
    settle(st.page)
    t = st.terminals["cli"]
    t.set_title("~")
    t.run("cd ~ && clear", r"\$ ")
    time.sleep(0.6)


def agents_act(st, cue):
    t = st.terminals["cli"]
    cue.at(cue.say[0] + 0.4)
    t.type("meertex projects", cps=15)
    time.sleep(0.3)
    t.send("\r")
    cue.at(cue.say[1] - 0.3)
    t.type(f"meertex threads {PAPER}", cps=17)
    time.sleep(0.3)
    t.send("\r")


def agents_camera(cue):
    return Camera([(0, 1.22, (768, 432)), (cue.length, 1.28, (768, 432))])


# 10. ending.


def ending_prepare(st):
    """The paper list again, without the pointer: a fresh page draws none until
    the mouse moves."""
    st.page.goto(f"{st.run.base}/")
    expect(st.page.locator(".next-date-when").first).to_be_visible(timeout=20000)
    settle(st.page)


def ending_camera(cue):
    return Camera([(0, 1.18, (800, 470)), (cue.length, 1.0, (768, 432))])


# Subtitles at the top, for shots whose action happens at the bottom.
SUBS_TOP = (8, 260, 260, 40)

# vim over the editor's PDF, the command line in the middle of the screen.
VIM_RIGHT = ("vim", (REC_W - TERMINALS["vim"][0] - 70, 330))
CLI_CENTRE = ("cli", ((REC_W - TERMINALS["cli"][0]) // 2, (REC_H - TERMINALS["cli"][1]) // 2))

SCENES = [
    Scene("intro", lead=1.2, shots=[
        Shot("home", says=2, prepare=home_prepare, act=home_act, camera=home_camera),
    ]),
    Scene("local", shots=[
        Shot("local", who="tomas", says=2, hold=10.0, prepare=local_prepare, act=local_act, camera=local_camera,
             terminal=VIM_RIGHT, still=0.8),
    ]),
    Scene("browser", shots=[
        Shot("coauthor", says=1, hold=8.5, prepare=coauthor_prepare, act=coauthor_act, camera=coauthor_camera,
             subs=SUBS_TOP),
        Shot("cite", says=1, hold=11.0, prepare=cite_prepare, act=cite_act, camera=cite_camera),
    ]),
    Scene("refs", shots=[
        Shot("refs", says=2, hold=9.0, prepare=refs_prepare, act=refs_act, camera=refs_camera, subs=SUBS_TOP),
    ]),
    Scene("venue", shots=[
        Shot("overview", says=1, hold=5.5, prepare=overview_prepare, act=overview_act, camera=overview_camera),
        Shot("question", says=2, hold=9.0, prepare=question_prepare, act=question_act, camera=question_camera),
        Shot("versions", hold=5.5, prepare=versions_prepare, act=versions_act, camera=versions_camera),
    ]),
    Scene("compare", shots=[
        Shot("pdfdiff", says=1, hold=6.0, prepare=pdfdiff_prepare, act=pdfdiff_act, camera=pdfdiff_camera),
    ]),
    Scene("review", shots=[
        Shot("review-files", who="priya", says=2, hold=14.0, prepare=review_files_prepare, act=review_files_act,
             camera=review_files_camera, subs=SUBS_TOP),
        Shot("review", who="priya", says=1, hold=6.0, prepare=review_prepare, act=review_act, camera=review_camera),
    ]),
    Scene("timeline", shots=[
        Shot("timeline", says=1, hold=6.0, prepare=timeline_prepare, camera=timeline_camera),
    ]),
    Scene("agents", shots=[
        Shot("agents", says=2, hold=10.0, prepare=agents_prepare, act=agents_act, camera=agents_camera,
             terminal=CLI_CENTRE),
    ]),
    Scene("ending", lead=1.6, tail=4.0, subtitles=False, shots=[
        Shot("ending", says=3, prepare=ending_prepare, camera=ending_camera),
    ]),
]


# --- the studio -----------------------------------------------------------------------


class Studio:
    """The server with the demo account, Lena's checkout with meertex sync, the
    screens, the browser window, the terminals and the coauthors' headless
    browsers."""

    def __init__(self, args, work: Path, pw):
        self.args, self.work = args, work
        self.home = work / "home"
        self.notes: list[str] = []
        self.marks: dict = {}
        self.who = None
        self.others = {}
        self.sync = None
        self.run = shots.Run(args)
        self.screen = None
        self.screens: list[stage.Screen] = []
        self.terminals: dict[str, stage.Terminal] = {}
        self.window = self.headless = None
        self.pw = pw

    def start(self):
        t0 = time.monotonic()
        self.run.start()
        self.run.make_people()
        self.run.seed()
        say(f"seeded {len(self.run.papers)} papers in {time.monotonic() - t0:.0f} s")
        self.seed_refs()
        self.checkout()
        profiles = self.work / "profiles"
        shutil.rmtree(profiles, ignore_errors=True)
        self.screen = stage.Screen(REC_W, REC_H)
        self.screens.append(self.screen)
        self.window = stage.Window(self.pw, self.screen, profiles / "main", self.home)
        for n, (name, (w, h)) in enumerate(TERMINALS.items()):
            screen = stage.Screen(w, h)
            self.screens.append(screen)
            window = stage.Window(self.pw, screen, profiles / f"terminal-{name}", self.home, pointer=False)
            term = stage.Terminal(window, self.home, self.cli_env(), self.home / ".bashrc", self.args.term_port + n)
            term.wait_for(r"\$ ")
            self.terminals[name] = term
        self.headless = self.pw.chromium.launch(channel="chrome", headless=True)

    def seed_refs(self):
        """The ESEM paper gets demo/refs.bib and a related work that cites it.
        Jonas checks the same file in a paper of his own first, so every lookup
        is in the server's cache and the check on camera takes seconds instead
        of half a minute."""
        bib = (HERE / "demo" / "refs.bib").read_bytes()
        with self.run.api("lena") as c:
            shots.Run.put_files(c, self.run.papers["metrics_esem"],
                                {"refs.bib": bib, "sections/related.tex": (HERE / "demo" / "related.tex").read_bytes()})
        t0 = time.monotonic()
        with self.run.api("jonas") as c:
            r = c.post("/api/projects", json={"title": "Reference check warm-up", "paper": "warmup",
                                              "conference": "Warmup", "year": 2027})
            r.raise_for_status()
            p = r.json()
            shots.Run.put_files(c, p, {"refs.bib": bib})
            r = c.post(f"/api/projects/{p['id']}/references/check", json={})
            r.raise_for_status()
            run = r.json()

            def finished():
                now = c.get(f"/api/projects/{p['id']}/references/check/{run['id']}").json()
                return now if now["status"] != "running" else None

            done = shots.wait_until(finished, timeout=300, interval=1, what="the warm-up reference check")
            report = c.get(f"/api/projects/{p['id']}/references").json()
        statuses = ", ".join(f"{e['key']} {e['status']}" for e in report["entries"])
        say(f"warmed up the reference check in {time.monotonic() - t0:.0f} s ({done['status']}): {statuses}")
        for e in report["entries"]:
            for f in e["findings"]:
                if f["level"] != "info":
                    say(f"  {e['key']}: {f['level']} {f['code']}: {f['message']}")

    def cli_env(self) -> dict:
        return {
            **stage.BASE_ENV,
            "HOME": str(self.home),
            "PATH": f"{self.home / '.local' / 'bin'}:/usr/local/bin:/usr/bin:/bin",
            "MEERTEX_HOME": str(self.home / ".meertex"),
            "MEERTEX_PAPERS": str(self.home / "papers"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "MEERTEX_SERVER": self.run.base,
            "MEERTEX_TOKEN": self.run.token["lena"],
        }

    def checkout(self):
        """Lena's home for the terminal: the meertex command, a prompt, vim's
        settings and her checkout of the EuroSys paper, kept in step by a
        running meertex sync."""
        if self.home.exists():
            shutil.rmtree(self.home)
        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / ".local" / "bin" / "meertex").symlink_to(MEERTEX)
        (self.home / ".bashrc").write_text(
            "PS1='\\[\\e[38;5;75m\\]\\w\\[\\e[0m\\] \\$ '\n"
            "export EDITOR=vim LESS=-FRX\n"
            "HISTFILE=/dev/null\n"
        )
        (self.home / ".vimrc").write_text(
            "set nocompatible\nsyntax on\nset background=dark\nset number\nset linebreak breakindent\n"
            "set noswapfile nobackup nowritebackup noundofile viminfo=\nset shortmess+=I\nset laststatus=0 ruler noshowcmd\n"
            "set nomodeline\n"
        )
        env = self.cli_env()
        subprocess.run([str(MEERTEX), "checkout", PAPER], env=env, cwd=self.home, check=True, capture_output=True)
        log = open(self.work / "logs" / "sync.log", "w")
        self.sync = subprocess.Popen([str(MEERTEX), "sync"], env=env, cwd=self.home, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        time.sleep(2.0)

    @property
    def page(self):
        return self.window.page

    def sign_in(self, who: str):
        if self.who != who:
            self.window.sign_in(self.run.base, self.run.session[who])
            self.who = who

    def other(self, who: str):
        """A coauthor in a headless browser of their own."""
        if who not in self.others:
            ctx = self.run.context(self.headless, who, CSS_W, CSS_H)
            self.others[who] = ctx.new_page()
        return self.others[who]

    def wait_for_version(self, key: str, kind: str):
        """Wait until the paper has a compiled version of this kind, answering
        its question first when a shot that makes it was left out."""
        p = self.run.papers[key]
        with self.run.api("lena") as c:
            project = c.get(f"/api/projects/{p['id']}").json()
            if not any(v["kind"] == kind for v in project["versions"]):
                self.run.answer(c, p, "rebuttal_end", "yes", version={"at": "deadline", "note": ""})

            def compiled():
                versions = c.get(f"/api/projects/{p['id']}").json()["versions"]
                return any(v["kind"] == kind and (v.get("compiled") or {}).get("status") in shots.DONE for v in versions)

            shots.wait_until(compiled, timeout=240, interval=1, what=f"the {kind} version's PDF")

    def stop(self):
        for page in self.others.values():
            page.context.close()
        if self.headless:
            self.headless.close()
        for term in self.terminals.values():
            term.stop()
            term.window.close()
        if self.window:
            self.window.close()
        for screen in self.screens:
            screen.stop()
        if self.sync and self.sync.poll() is None:
            self.sync.send_signal(2)
            try:
                self.sync.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.sync.kill()
        self.run.stop()

    # --- filming ------------------------------------------------------------------

    def film(self, scene: Scene, shot: Shot):
        say(f"filming {shot.id}: {shot.frames / FPS:.1f} s")
        self.sign_in(shot.who)
        if shot.prepare:
            shot.prepare(self)
        length = shot.frames / FPS
        raw = self.work / "raw"
        raw.mkdir(exist_ok=True)
        seconds = PREROLL + length + 0.6
        recs = [stage.Recording(self.screen, raw / f"{shot.id}.mkv", seconds)]
        if shot.terminal:
            term = self.terminals[shot.terminal[0]]
            recs.append(stage.Recording(term.window.screen, raw / f"{shot.id}-terminal.mkv", seconds))
        time.sleep(PREROLL)
        t0, wall = time.monotonic(), time.time()
        cue = Cue(t0, shot.cues, length)
        if shot.act:
            shot.act(self, cue)
        late = cue.now() - length
        if late > 0:
            self.notes.append(f"{shot.id}: the action ran {late:.1f} s past the end of the shot")
        for rec in recs:
            rec.wait()
        camera = shot.camera(cue) if shot.camera else Camera.still()
        clip = self.work / "clips" / f"{shot.id}.mkv"
        still = self.work / "stills" / f"{shot.id}.png"
        extra = {}
        if shot.terminal:
            name, at = shot.terminal
            extra = dict(term=recs[1].path, term_skip=wall - recs[1].started(), term_at=at,
                         term_size=TERMINALS[name], mask=self.work / f"mask-{name}.png",
                         shadow=self.work / f"shadow-{name}.png", shadow_pad=SHADOW_PAD)
        stage.develop(recs[0].path, wall - recs[0].started(), shot.frames, camera, clip, still,
                      round((shot.frames - 1) * shot.still), **extra)
        if not self.args.keep_raw:
            for rec in recs:
                rec.path.unlink(missing_ok=True)


# --- narration and timing ---------------------------------------------------------------


def say(msg: str):
    print(f"[video] {msg}", flush=True)


def read_narration() -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    """The sentences of every scene as (subtitle, spoken) and the lexicon of
    [pronounce]. A line `subtitle | spoken` speaks other words than it shows."""
    scenes: dict[str, list[tuple[str, str]]] = {}
    lexicon: dict[str, str] = {}
    current = None
    for line in (HERE / "narration.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if current != "pronounce":
                scenes[current] = []
        elif current == "pronounce":
            word, _, phonemes = line.partition("=")
            lexicon[word.strip()] = phonemes.strip()
        elif current:
            shown, _, spoken = line.partition("|")
            scenes[current].append((shown.strip(), (spoken or shown).strip()))
    return scenes, lexicon


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def kokoro_models(given: str | None) -> Path:
    for folder in ([Path(given).expanduser()] if given else KOKORO_MODELS):
        if (folder / "kokoro-v1.0.onnx").exists() and (folder / "voices-v1.0.bin").exists():
            return folder
    raise SystemExit("Kokoro's model files are missing. Put kokoro-v1.0.onnx and voices-v1.0.bin into "
                     f"{KOKORO_MODELS[0]} or pass --kokoro-models (see kokoro_say.py).")


def speak(audio: Path, kokoro_python: str | None, models: str | None):
    """Speaks every sentence into its own WAV file. A sentence keeps its
    recording while its text and voice stay the same, because Kokoro varies a
    little from run to run and the shots must keep fitting."""
    scenes, lexicon = read_narration()
    audio.mkdir(parents=True, exist_ok=True)
    jobs = []
    for scene in SCENES:
        scene.sentences = []
        for i, (shown, spoken) in enumerate(scenes[scene.id]):
            path = audio / f"{scene.id}-{i + 1}.wav"
            used = {k: v for k, v in lexicon.items() if k.lower() in spoken.lower()}
            key = {"voice": KOKORO_VOICE, "speed": KOKORO_SPEED, "text": spoken, "lexicon": used}
            said = path.with_suffix(".json")
            if not (path.exists() and said.exists() and json.loads(said.read_text()) == key):
                jobs.append((path, said, key))
            scene.sentences.append((shown, path, 0.0))
    if jobs:
        if not kokoro_python:
            raise SystemExit("Kokoro needs a Python with kokoro-onnx: pass --kokoro-python or set "
                             "MEERTEX_VIDEO_KOKORO_PYTHON (see kokoro_say.py).")
        spec = {
            "models": str(kokoro_models(models)),
            "voice": KOKORO_VOICE,
            "speed": KOKORO_SPEED,
            "lexicon": lexicon,
            "jobs": [{"text": key["text"], "out": str(path)} for path, _, key in jobs],
        }
        spec_path = audio / "kokoro-jobs.json"
        stage.write_json(spec_path, spec)
        say(f"speaking {len(jobs)} sentences with Kokoro ({KOKORO_VOICE}, speed {KOKORO_SPEED})")
        out = subprocess.run([kokoro_python, "-I", str(HERE / "kokoro_say.py"), str(spec_path)],
                             capture_output=True, text=True, check=True).stdout
        spoken_as = {json.loads(line)["out"]: json.loads(line)["phonemes"] for line in out.splitlines() if line.startswith("{")}
        for path, said, key in jobs:
            said.write_text(json.dumps(key, ensure_ascii=False))
            path.with_suffix(".phonemes").write_text(spoken_as.get(str(path), "") + "\n")
    for scene in SCENES:
        scene.sentences = [(shown, path, duration(path)) for shown, path, _ in scene.sentences]


def plan():
    """Lays out every scene shot by shot. A shot speaks its sentences with GAP
    seconds between them, ends SHOT_TAIL after its last sentence (the scene's
    tail for the last shot) and never runs shorter than its hold."""
    for scene in SCENES:
        scene.offsets = []
        t, i = 0.0, 0
        for n, shot in enumerate(scene.shots):
            start = t
            last = n == len(scene.shots) - 1
            cursor = start + (scene.lead if n == 0 else SHOT_LEAD)
            shot.cues = []
            for _ in range(shot.says):
                scene.offsets.append(cursor)
                shot.cues.append(cursor - start)
                cursor += scene.sentences[i][2] + GAP
                i += 1
            end = start + shot.hold
            if shot.says:
                end = max(end, cursor - GAP + (scene.tail if last else SHOT_TAIL))
            elif last:
                end = max(end, start + scene.tail)
            shot.start = start
            shot.frames = round((end - start) * FPS)
            t = start + shot.frames / FPS
        if i != len(scene.sentences):
            raise SystemExit(f"scene {scene.id} has {len(scene.sentences)} sentences in narration.txt, but its shots speak {i}")
        scene.frames = sum(s.frames for s in scene.shots)


def scene_starts() -> list[float]:
    starts, t = [], 0.0
    for scene in SCENES:
        starts.append(t)
        t += scene.frames / FPS - XFADE
    return starts


def total_seconds() -> float:
    return sum(s.frames for s in SCENES) / FPS - XFADE * (len(SCENES) - 1)


def storyboard() -> str:
    rows = ["| # | Scene | Shots | Seconds |", "| --- | --- | --- | --- |"]
    for i, (start, scene) in enumerate(zip(scene_starts(), SCENES), 1):
        shot_list = ", ".join(f"{s.id} ({s.frames / FPS:.1f} s)" for s in scene.shots)
        rows.append(f"| {i} | {scene.id} | {shot_list} | {start:.1f} to {start + scene.frames / FPS:.1f} |")
    return "\n".join(rows)


# --- sound, subtitles, the end card and the final video ----------------------------------


def narration_track(audio: Path) -> Path:
    """Every sentence at its place on one track."""
    rate = None
    total = total_seconds()
    samples = None
    for start, scene in zip(scene_starts(), SCENES):
        for offset, (_, path, _) in zip(scene.offsets, scene.sentences):
            with wave.open(str(path)) as f:
                if rate is None:
                    rate = f.getframerate()
                    samples = array.array("h", bytes(2 * int(total * rate + rate)))
                assert f.getframerate() == rate and f.getnchannels() == 1 and f.getsampwidth() == 2
                data = array.array("h", f.readframes(f.getnframes()))
            at_sample = int((start + offset) * rate)
            end = min(len(samples), at_sample + len(data))
            samples[at_sample:end] = data[: end - at_sample]
    path = audio / "narration.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(samples.tobytes())
    return path


def pad_track(audio: Path, seconds: float) -> Path:
    """A quiet, slowly breathing D major chord with a little brown noise."""
    path = audio / "pad.wav"
    tones = [(73.42, 0.5), (110.0, 0.35), (146.83, 0.25), (185.0, 0.16), (220.0, 0.1)]
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for f, _ in tones:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={f}:sample_rate=48000:duration={seconds}"]
    cmd += ["-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.6:sample_rate=48000:duration={seconds}"]
    mix = "".join(f"[{i}]volume={v}[t{i}];" for i, (_, v) in enumerate(tones))
    n = len(tones)
    mix += f"[{n}]lowpass=f=260,volume=0.5[t{n}];"
    mix += "".join(f"[t{i}]" for i in range(n + 1))
    mix += (
        f"amix=inputs={n + 1}:normalize=0,tremolo=f=0.12:d=0.5,lowpass=f=1400,volume=0.11,"
        f"afade=t=in:d=3,afade=t=out:st={seconds - 4:.2f}:d=4"
    )
    subprocess.run(cmd + ["-filter_complex", mix, "-ac", "2", str(path)], check=True)
    return path


def ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def subtitles(work: Path) -> Path:
    """Burned-in subtitles in the app's colours: white Noto Sans on a panel of
    the app's text colour (#1f2328)."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Meertex,Noto Sans SemiBold,42,&H00FFFFFF,&H00FFFFFF,&H1A28231F,&H00000000,0,0,0,0,100,100,0,0,3,16,0,2,260,260,64,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for start, scene in zip(scene_starts(), SCENES):
        if not scene.subtitles:
            continue
        # Each shot's span in the video, so a sentence that runs over a cut moves with it.
        spans, t = [], start
        for shot in scene.shots:
            spans.append((t, t + shot.frames / FPS, shot.subs))
            t += shot.frames / FPS
        for offset, (text, _, d) in zip(scene.offsets, scene.sentences):
            begin = start + offset - 0.05
            end = start + offset + d + 0.5
            for a, b, subs in spans:
                lo, hi = max(begin, a), min(end, b)
                if hi - lo < 0.04:
                    continue
                if subs:
                    align, left, right, bottom = subs
                    rows.append(f"Dialogue: 0,{ass_time(lo)},{ass_time(hi)},Meertex,,{left},{right},{bottom},,{{\\an{align}}}{text}")
                else:
                    rows.append(f"Dialogue: 0,{ass_time(lo)},{ass_time(hi)},Meertex,,0,0,0,,{text}")
    path = work / "subtitles.ass"
    path.write_text(head + "\n".join(rows) + "\n")
    return path


NOTO = Path("/usr/share/fonts/google-noto")


def card(work: Path) -> tuple[Path, Path]:
    """The end card in two layers, white like the app: the meerkat, the name and
    the line of the landing page, then "Part of the Meerverse" and the address,
    which fade in later, as on meerora's end card."""
    from PIL import Image, ImageDraw, ImageFont

    first, second = work / "card.png", work / "card-address.png"
    bold = ImageFont.truetype(str(NOTO / "NotoSans-Bold.ttf"), 104)
    line = ImageFont.truetype(str(NOTO / "NotoSans-SemiBold.ttf"), 46)
    label = ImageFont.truetype(str(NOTO / "NotoSansMono-Medium.ttf"), 25)
    address = ImageFont.truetype(str(NOTO / "NotoSans-SemiBold.ttf"), 46)

    img = Image.new("RGBA", (OUT_W, OUT_H), (255, 255, 255, 240))
    meerkat = Image.open(STATIC / "img" / "meerkat-1254.png").convert("RGBA").resize((330, 330), Image.LANCZOS)
    img.alpha_composite(meerkat, ((OUT_W - 330) // 2, 120))
    draw = ImageDraw.Draw(img)
    draw.text((OUT_W / 2, 500), "meertex", font=bold, fill="#1f2328", anchor="mt")
    a, b = "Write LaTeX locally. ", "Your coauthors see it live."
    wa, wb = draw.textlength(a, font=line), draw.textlength(b, font=line)
    x = (OUT_W - wa - wb) / 2
    draw.text((x, 650), a, font=line, fill="#1f2328", anchor="lt")
    draw.text((x + wa, 650), b, font=line, fill="#7a4a1f", anchor="lt")
    img.save(first)

    img = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle(((OUT_W - 72) / 2, 748, (OUT_W + 72) / 2, 750), fill="#d1d9e0")
    text = "PART OF THE MEERVERSE"
    spaced = sum(draw.textlength(c, font=label) for c in text) + 3 * (len(text) - 1)
    x = (OUT_W - spaced) / 2
    for c in text:
        draw.text((x, 776), c, font=label, fill="#59636e", anchor="lt")
        x += draw.textlength(c, font=label) + 3
    draw.text((OUT_W / 2, 820), "meertex.com", font=address, fill="#0969da", anchor="mt")
    img.save(second)
    return first, second


def assemble(work: Path, audio: Path, out: Path):
    say("putting the video together")
    total = total_seconds()
    starts = scene_starts()
    inputs: list[str] = []
    graph: list[str] = []
    k = 0
    for n, scene in enumerate(SCENES):
        labels = []
        for shot in scene.shots:
            inputs += ["-i", str(work / "clips" / f"{shot.id}.mkv")]
            labels.append(f"[{k}:v]")
            k += 1
        norm = f"fps={FPS},format=yuv444p,setsar=1,settb=AVTB"
        if len(labels) == 1:
            graph.append(f"{labels[0]}{norm}[s{n}]")
        else:
            graph.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,{norm}[s{n}]")
    prev = "s0"
    for n in range(1, len(SCENES)):
        offset = sum(s.frames for s in SCENES[:n]) / FPS - XFADE * n
        graph.append(f"[{prev}][s{n}]xfade=transition=fade:duration={XFADE}:offset={offset:.4f}[x{n}]")
        prev = f"x{n}"
    (card_png, address_png), narration, pad = card(work), narration_track(audio), pad_track(audio, total)
    loop = ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}"]
    inputs += [*loop, "-i", str(card_png), *loop, "-i", str(address_png), "-i", str(narration), "-i", str(pad)]
    card_in, address_in, narr_in, pad_in = k, k + 1, k + 2, k + 3
    ending = SCENES[-1]
    card_at = starts[-1] + ending.lead - 0.6
    # "Part of the Meerverse" comes in with the sentence that says it.
    said = next((i for i, (text, _, _) in enumerate(ending.sentences) if "Meerverse" in text), len(ending.offsets) - 1)
    address_at = starts[-1] + ending.offsets[said] - 0.3
    subs = subtitles(work)
    graph.append(f"[{card_in}:v]format=rgba,fade=t=in:st={card_at:.3f}:d=0.9:alpha=1[card]")
    graph.append(f"[{address_in}:v]format=rgba,fade=t=in:st={address_at:.3f}:d=0.8:alpha=1[address]")
    graph.append(
        f"[{prev}]format=rgba[base];[base][card]overlay=0:0:shortest=1[carded];[carded][address]overlay=0:0:shortest=1,"
        f"ass={subs}:fontsdir={NOTO},"
        f"fade=t=in:st=0:d=0.8,fade=t=out:st={total - 1.0:.3f}:d=1.0,format=yuv420p[v]"
    )
    graph.append(
        f"[{narr_in}:a]aresample=48000,aformat=channel_layouts=stereo,apad[voice];"
        f"[{pad_in}:a]aformat=channel_layouts=stereo[pad];"
        # Loudness of web video, about -17 LUFS, with the peaks kept below -1.5 dBTP.
        f"[voice][pad]amix=inputs=2:normalize=0:duration=longest,atrim=0:{total:.3f},"
        f"loudnorm=I=-17:TP=-1.5:LRA=11,aresample=48000,afade=t=out:st={total - 1.2:.3f}:d=1.2[a]"
    )
    tmp = out.with_name(out.stem + ".part.mp4")
    cmd = ["ffmpeg", "-v", "error", "-stats", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]"]
    cmd += ["-c:v", "libopenh264", "-profile:v", "high", "-b:v", "12M", "-maxrate", "18M", "-g", "60", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.3f}", str(tmp)]
    (work / "ffmpeg-assemble.txt").write_text(" ".join(cmd) + "\n")
    subprocess.run(cmd, check=True)
    tmp.rename(out)


def free_name(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 100):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise SystemExit(f"no free name next to {path}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--work", type=Path, default=Path.home() / ".cache" / "meertex-video",
                   help="folder for the recordings, the clips, the audio and the stills (~/.cache/meertex-video). "
                        "It needs a few GB, so not a tmpfs")
    p.add_argument("--out", type=Path, default=Path.home() / "Videos" / "meertex-tour.mp4",
                   help="the video. An existing file is never replaced, -2 is added instead")
    p.add_argument("--only", help="film only these shots, comma separated, and skip the final video")
    p.add_argument("--reuse", action="store_true", help="keep the clips in WORK/clips and film only the missing shots")
    p.add_argument("--plan", action="store_true", help="speak the narration, print the storyboard and stop")
    p.add_argument("--kokoro-python", default=os.environ.get("MEERTEX_VIDEO_KOKORO_PYTHON"),
                   help="a Python with kokoro-onnx, also MEERTEX_VIDEO_KOKORO_PYTHON")
    p.add_argument("--kokoro-models", help=f"the folder with Kokoro's model files (default: {KOKORO_MODELS[0]})")
    p.add_argument("--db", default=os.environ.get("MEERTEX_VIDEO_DB", DEFAULT_DB),
                   help="the database, emptied first. Its name must end in _website")
    p.add_argument("--port", type=int, default=8069, help="the server's port (8069)")
    p.add_argument("--compile-port", type=int, default=8079, help="the compile service's port (8079)")
    p.add_argument("--term-port", type=int, default=8089,
                   help="the port of the first terminal page, the next one takes the port after it (8089)")
    p.add_argument("--keep-raw", action="store_true", help="keep the screen recordings in WORK/raw")
    args = p.parse_args()
    args.keep_tmp = False

    for tool in ("Xvfb", "ffmpeg", "ffprobe", "setsid", "vim", "magick"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is missing")
    if not (stage.XTERM / "lib" / "xterm.js").exists():
        raise SystemExit(f"xterm.js is missing. Run npm ci in {HERE}.")
    work = args.work.expanduser().resolve()
    for d in ("clips", "stills", "audio", "logs", "server"):
        (work / d).mkdir(parents=True, exist_ok=True)
    args.tmp = str(work / "server")
    say(f"working in {work}")

    speak(work / "audio", args.kokoro_python, args.kokoro_models)
    plan()
    print(storyboard(), flush=True)
    say(f"{total_seconds():.1f} s in total")
    if args.plan:
        return

    all_shots = [(scene, shot) for scene in SCENES for shot in scene.shots]
    only = set(args.only.split(",")) if args.only else None
    unknown = (only or set()) - {s.id for _, s in all_shots}
    if unknown:
        raise SystemExit(f"no such shot: {', '.join(sorted(unknown))}")
    if args.reuse and not only:
        only = {s.id for _, s in all_shots if not (work / "clips" / f"{s.id}.mkv").exists()}
    todo = [(scene, shot) for scene, shot in all_shots if only is None or shot.id in only]

    if todo:
        for name, (w, h) in TERMINALS.items():
            stage.window_mask(work / f"mask-{name}.png", w, h, TERM_RADIUS)
            stage.window_shadow(work / f"shadow-{name}.png", w, h, TERM_RADIUS, SHADOW_PAD)
        with sync_playwright() as pw:
            studio = Studio(args, work, pw)
            try:
                studio.start()
                for scene, shot in todo:
                    studio.film(scene, shot)
            finally:
                studio.stop()
                for note in studio.notes:
                    say(note)
    if args.only:
        return
    out = free_name(args.out.expanduser())
    out.parent.mkdir(parents=True, exist_ok=True)
    assemble(work, work / "audio", out)
    say(f"wrote {out} ({duration(out):.1f} s)")


if __name__ == "__main__":
    main()
