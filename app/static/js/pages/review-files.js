// The Files view of a review (docs/DESIGN.md §13, In the web app, and §14).
//
// The source at the review's commit with line numbers, like the Files tab.
// Threads whose base is that commit sit under their lines, their spans marked
// in the text. That is this review's threads, single comments and other
// reviews' threads on the same state. Threads of this review made later on
// the live text are listed above, because their lines are another state's.
//
// Selecting text shows a "Comment" button, and a click on a line number (a
// shift-click for a range) opens the comment box at once. The box can add a
// single comment, start a review or add to the pending one, and suggest new
// text. Next to the source is the PDF of the commit. A double-click there
// asks SyncTeX for the source line and opens the comment box at it, and
// picking a thread marks its lines in the PDF. Text selected in the PDF is
// marked in the source, with a "Comment" button next to the selection.
//
// Like the editor, the source and the PDF fill the window below the review's
// header and scroll on their own, and LaTeX has the editor's colours.

import { api, enc, encPath, projectUrl } from "../api.js";
import { h, clear, debounce, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave, setQuery } from "../router.js";
import { createPdfView } from "../pdfview.js";
import { findPassage } from "../editor-spot.js";
import { session } from "../session.js";
import { blankslate, flash, flashBox, flashError, queueFlash, spinner } from "../ui.js";
import {
  FileTexts, commentForm, linesText, refreshOpenCounts, threadCard, toCodePoints, toUtf16,
} from "../threads.js";

const PDF_KEY = "meertex.review.pdf";
const PDF_WIDTH_KEY = "meertex.review.pdfwidth";
const FINISHED = new Set(["success", "errors", "failure", "timeout", "unavailable"]);
const PHONE = "(max-width: 767px)";
// The editor's bundle (pages/editor.js) and the files it colours, for the same colours here.
const BUNDLE = "/static/vendor/editor/editor.js";
const LATEX = /\.(tex|sty|cls|ltx|dtx|ins|bbx|cbx|lbx|tikz)$/i;

let colours = null;

// --- PDF helpers, shared with the Changes view ---------------------------------------

// POST a compile route and wait until the compile is finished.
export async function compileWait(ctx, path, body) {
  let c = await api.post(projectUrl(ctx.ref, path), body);
  while (c && !FINISHED.has(c.status)) {
    await new Promise((r) => setTimeout(r, 1000));
    if (!ctx.alive()) return null;
    c = await api.get(projectUrl(ctx.ref, `/compiles/${enc(c.id)}`));
  }
  return c;
}

// A box with a PDF in the viewer, its zoom, page count and a download link.
export function pdfBox(host, { title, url, name, onDoubleClick = null, onSelect = null, extra = null, cls = "" }) {
  const zoom = h("span", { class: "pdf-zoom-text", text: "" });
  const pages = h("span", { class: "pdf-pages" });
  const view = { current: null };
  const header = h("div", { class: "box-header box-header-sm" },
    icon("pdf", { cls: "color-muted" }),
    h("strong", { class: "flex-1 truncate", text: title }),
    extra,
    h("button", { type: "button", class: "btn-octicon pdf-zoom-step", "aria-label": "Zoom out", text: "−", onclick: () => view.current.zoomOut() }),
    zoom,
    h("button", { type: "button", class: "btn-octicon pdf-zoom-step", "aria-label": "Zoom in", text: "+", onclick: () => view.current.zoomIn() }),
    h("button", { type: "button", class: "btn btn-sm", text: "Fit", onclick: () => view.current.setZoom("width") }),
    pages,
    h("a", { class: "btn-octicon", href: url, download: name, "aria-label": "Download the PDF", title: "Download the PDF" }, icon("download")));
  const body = h("div", { class: "pdf-box-body" });
  clear(host).append(h("div", { class: `box pdf-box ${cls}`.trim() }, header, body));
  view.current = createPdfView(body, {
    onDoubleClick,
    onSelect,
    onChange: ({ page, pages: n, zoom: z }) => {
      zoom.textContent = `${Math.round(z * 100)}%`;
      pages.textContent = n ? `${page} / ${n}` : "";
    },
  });
  view.current.load(url).catch((err) => {
    console.error(err);
    clear(body).append(blankslate({ iconName: "alert", title: "The PDF could not be shown", text: err.message || String(err) }));
  });
  return view.current;
}

function stored(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Not remembered, which is fine.
  }
}

// Without a choice, the width decides.
function readPdfPref() {
  const v = stored(PDF_KEY);
  return v === "on" || v === "off" ? v === "on" : window.matchMedia("(min-width: 1012px)").matches;
}

// The PDF's share of the width.
function readPdfWidth() {
  const v = Number(stored(PDF_WIDTH_KEY));
  return v >= 0.15 && v <= 0.85 ? v : 0.46;
}

// The spans of a LaTeX file to colour, [{from, to, cls}] in UTF-16 units: the
// editor's mode, in its colours (the .tex-* rules of app.css). Other files,
// and LaTeX when the bundle does not load, stay plain.
async function texColours(path, text) {
  if (!LATEX.test(path)) return [];
  colours ??= import(BUNDLE).then((cm) => {
    const t = cm.tags;
    return {
      cm,
      language: cm.StreamLanguage.define(cm.stex),
      highlighter: cm.tagHighlighter([
        { tag: t.comment, class: "tex-comment" },
        { tag: t.tagName, class: "tex-command" },
        { tag: t.atom, class: "tex-atom" },
        { tag: [t.keyword, t.special(t.variableName), t.number], class: "tex-keyword" },
        { tag: t.bracket, class: "tex-bracket" },
        { tag: t.string, class: "tex-string" },
        { tag: t.invalid, class: "tex-invalid" },
      ]),
    };
  }).catch((err) => {
    colours = null;
    throw err;
  });
  try {
    const { cm, language, highlighter } = await colours;
    const spans = [];
    cm.highlightTree(language.parser.parse(text), highlighter, (from, to, cls) => spans.push({ from, to, cls }));
    return spans;
  } catch (err) {
    console.warn("The LaTeX could not be coloured", err);
    return [];
  }
}

function splitLines(text) {
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const starts = [];
  let off = 0;
  for (const l of lines) {
    starts.push(off);
    off += l.length + 1;
  }
  return { lines, starts };
}

// --- The view ---------------------------------------------------------------------------

export async function renderFiles(ctx) {
  const { review, perms } = ctx;
  const p = ctx.project;
  const commit = review.commit;
  const [files, allThreads, reviews] = await Promise.all([
    api.get(projectUrl(ctx.ref, "/files"), { query: { ref: commit } }),
    api.get(projectUrl(ctx.ref, "/threads"), { query: { state: "all" } }),
    api.get(projectUrl(ctx.ref, "/reviews")),
  ]);
  if (!ctx.alive()) return;

  const textFiles = files.filter((f) => f.kind === "text").sort((a, b) => a.path.localeCompare(b.path));
  const myPending = reviews.find((r) => r.state === "pending" && r.reviewer?.id === session.me?.id) || null;

  // Every thread this view knows: those placed on this commit, and this review's own.
  const known = new Map();
  for (const t of allThreads) if (t.base.commit === commit) known.set(t.id, t);
  for (const t of review.threads) known.set(t.id, t);
  const placed = (path) => [...known.values()]
    .filter((t) => t.base.commit === commit && t.base.path === path)
    .sort((a, b) => a.base.start - b.base.start || a.created_at.localeCompare(b.created_at));
  const later = () => [...known.values()].filter((t) => t.review_id === review.id && t.base.commit !== commit);

  const counts = new Map();
  const countFiles = () => {
    counts.clear();
    for (const t of known.values()) if (t.base.commit === commit) counts.set(t.base.path, (counts.get(t.base.path) || 0) + 1);
  };
  countFiles();

  const wanted = ctx.query.get("path");
  let startPath = textFiles.some((f) => f.path === wanted) ? wanted : null;
  startPath ??= textFiles.find((f) => counts.get(f.path))?.path
    ?? textFiles.find((f) => f.path === p.main_file)?.path
    ?? textFiles.find((f) => /\.tex$/i.test(f.path))?.path
    ?? textFiles[0]?.path
    ?? null;

  if (!startPath) {
    clear(ctx.main).append(h("div", { class: "box" }, blankslate({
      iconName: "file", title: "No text files in this state", text: "There is nothing here to comment on.",
    })));
    return;
  }

  // pick is the text selected in the PDF as found in the source, {s16, e16} or
  // {lines: true, line_start, line_end}.
  const state = {
    path: startPath, text: "", lines: [], starts: [], colours: [], compose: null, anchorLine: null, selected: null, pending: null, pick: null,
  };
  const commitTexts = new FileTexts(p.id, commit);
  const cards = new Map();
  const pdf = { on: readPdfPref(), started: false, view: null, compile: null };

  const env = {
    project: p,
    base: ctx.base,
    perms,
    texts: new FileTexts(p.id, "live"),
    reviews: new Map(reviews.map((r) => [r.id, r])),
    onChange: (next, old) => {
      if (next) known.set(next.id, next);
      else {
        known.delete(old.id);
        cards.delete(old.id);
        countFiles();
        paintSelect();
        paintCode();
      }
      ctx.setCounts([...known.values()]);
      refreshOpenCounts(p.id);
    },
    onSelect: (t, card) => selectThread(t, card),
  };

  // --- Toolbar -------------------------------------------------------------------------

  const select = h("select", { class: "form-select rv-file-select", "aria-label": "File" });
  const paintSelect = () => {
    clear(select).append(...textFiles.map((f) => {
      const n = counts.get(f.path) || 0;
      return h("option", { value: f.path }, n ? `${f.path} · ${plural(n, "comment")}` : f.path);
    }));
    select.value = state.path;
  };
  paintSelect();
  select.addEventListener("change", () => loadFile(select.value));

  const pdfToggle = h("button", { type: "button", class: "btn btn-sm rv-pdf-toggle", "aria-pressed": String(pdf.on), title: "Show the PDF of this state next to the source" },
    icon("pdf"), "PDF");
  pdfToggle.addEventListener("click", () => setPdf(!pdf.on));

  const lineCount = h("span", { class: "text-small color-muted nowrap" });
  const raw = h("a", { class: "btn btn-sm", target: "_blank", rel: "noopener", text: "Raw" });
  const toolbar = h("div", { class: "rv-files-toolbar" },
    select,
    h("span", { class: "text-small color-muted nowrap" }, "at ",
      h("a", { class: "sha", href: `${ctx.base}/commit/${enc(commit)}`, text: commit.slice(0, 7) })),
    h("span", { class: "flex-1" }),
    lineCount,
    raw,
    pdfToggle);

  const hint = perms.comment
    ? h("p", { class: "text-small color-muted rv-files-hint" },
      "Select text to comment on it, or click a line number. Shift-click a second one for a range. ",
      h("span", { class: "rv-pdf-hint" }, "Select text in the PDF to find it here, or double-click the PDF to comment on that line."))
    : flashBox(h("span", {}, "You can read this review. Commenting needs the reviewer, editor or owner role. ",
      h("span", { class: "rv-pdf-hint" }, "Select text in the PDF to find it here.")), "info");

  // --- Threads made on a later state -----------------------------------------------------

  const laterHost = h("div");
  const paintLater = () => {
    const list = later();
    clear(laterHost);
    if (!list.length) return;
    laterHost.append(h("div", { class: "rv-later mb-4" },
      h("div", { class: "rv-section-head" }, h("h3", { text: "Made on a later state" })),
      h("p", { class: "text-small color-muted", text: "These comments of the review were made on the live text after the review started, so their lines belong to another state. They are shown with the text as it reads now." }),
      list.map((t) => cardFor(t, { snippet: true }))));
  };

  // --- Source ---------------------------------------------------------------------------

  const tbody = h("tbody");
  const table = h("table", { class: "code-table rv-code-table" }, tbody);
  const floatBtn = h("button", { type: "button", class: "btn btn-sm btn-primary rv-float", hidden: true }, icon("comment", { size: 14 }), "Comment");
  const codeScroll = h("div", { class: "rv-code-scroll" }, h("div", { class: "rv-code-notes" }, hint, laterHost), table, floatBtn);
  const codeCol = h("section", { class: "rv-code-col", "aria-label": "Source" }, toolbar, codeScroll);
  const pdfFloat = h("button", { type: "button", class: "btn btn-sm btn-primary rv-pdf-float", hidden: true }, icon("comment", { size: 14 }), "Comment");
  const pdfCol = h("div", { class: "rv-pdf-col" });
  const divider = h("div", {
    class: "editor-divider rv-divider", role: "separator", "aria-orientation": "vertical", "aria-label": "Width of the PDF",
    tabindex: "0", title: "Drag to change the width. A double-click makes both halves equal.",
  });
  const split = h("div", { class: "rv-files-split" }, codeCol, divider, pdfCol);
  pdfCol.style.flexBasis = `${readPdfWidth() * 100}%`;

  function cardFor(t, opts = {}) {
    let card = cards.get(t.id);
    if (!card) {
      card = threadCard(t, env, {
        snippet: false,
        baseLines: true,
        reviewCommit: commit,
        showReview: t.review_id !== review.id,
        ...opts,
      });
      cards.set(t.id, card);
    }
    return card;
  }

  function paintHead() {
    lineCount.textContent = plural(state.lines.length, "line");
    raw.href = `${projectUrl(ctx.ref, `/files/${encPath(state.path)}`)}?ref=${enc(commit)}`;
  }

  function paintCode() {
    const here = placed(state.path);
    const spans = here.map((t) => ({
      id: t.id,
      s: toUtf16(state.text, t.base.start),
      e: toUtf16(state.text, t.base.end),
      first: t.base.line_start,
      last: t.base.line_end,
    }));
    const byEnd = new Map();
    here.forEach((t) => byEnd.set(t.base.line_end, [...(byEnd.get(t.base.line_end) || []), t]));
    const c = state.compose;
    const pick = state.pick && !state.pick.lines ? state.pick : null;
    const pickedLines = state.pick?.lines ? state.pick : null;
    const tokens = state.colours;
    let next = 0;
    const rows = [];
    state.lines.forEach((line, i) => {
      const n = i + 1;
      const ls = state.starts[i];
      const le = ls + line.length;
      const cuts = new Set([ls, le]);
      const cut = (s, e) => {
        if (s < le && e > ls) {
          cuts.add(Math.max(ls, s));
          cuts.add(Math.min(le, e));
        }
      };
      for (const sp of spans) cut(sp.s, sp.e);
      if (pick) cut(pick.s16, pick.e16);
      // The colours run in order, so each line starts where the last one stopped.
      while (next < tokens.length && tokens[next].to <= ls) next += 1;
      const coloured = [];
      for (let k = next; k < tokens.length && tokens[k].from < le; k++) {
        coloured.push(tokens[k]);
        cut(tokens[k].from, tokens[k].to);
      }
      const sorted = [...cuts].sort((a, b) => a - b);
      const content = [];
      for (let k = 0; k < sorted.length - 1; k++) {
        const a = sorted[k];
        const b = sorted[k + 1];
        if (a >= b) continue;
        const seg = state.text.slice(a, b);
        const colour = coloured.find((tk) => tk.from <= a && tk.to >= b);
        let node = colour ? h("span", { class: colour.cls, text: seg }) : seg;
        if (pick && pick.s16 <= a && pick.e16 >= b) node = h("span", { class: "rv-pick" }, node);
        const over = spans.filter((sp) => sp.s <= a && sp.e >= b && sp.s < sp.e);
        content.push(over.length
          ? h("mark", { class: `rv-mark${over.length > 1 ? " is-multi" : ""}`, dataset: { thread: over[over.length - 1].id }, title: "Show the comment" }, node)
          : node);
      }
      const marked = spans.some((sp) => n >= sp.first && n <= sp.last);
      const chosen = c && n >= c.line_start && n <= c.line_end;
      const picked = pickedLines && n >= pickedLines.line_start && n <= pickedLines.line_end;
      rows.push(h("tr", { class: ["rv-line", marked && "has-thread", chosen && "is-chosen", picked && "is-picked"], id: `RL${n}`, dataset: { line: n } },
        h("td", perms.comment
          ? { class: "ln rv-ln", dataset: { ln: n }, tabindex: "0", role: "button", "aria-label": `Comment on line ${n}`, title: "Comment on this line. Shift-click for a range." }
          : { class: "ln", dataset: { ln: n } }),
        h("td", { class: "lc", dataset: { off: ls } }, content)));
      for (const t of byEnd.get(n) || []) {
        rows.push(h("tr", { class: "rv-thread-row" }, h("td", { colspan: "2" }, cardFor(t))));
      }
      if (c && c.line_end === n) rows.push(h("tr", { class: "rv-compose-row" }, h("td", { colspan: "2" }, c.el)));
    });
    tbody.replaceChildren(...rows);
    if (state.selected) cards.get(state.selected)?.classList.add("is-selected");
  }

  async function loadFile(path, { keepCompose = false } = {}) {
    state.path = path;
    select.value = path;
    setQuery({ path });
    if (!keepCompose) state.compose = null;
    state.anchorLine = null;
    state.pick = null;
    hideFloat();
    tbody.replaceChildren(h("tr", {}, h("td", { colspan: "2", class: "rv-code-loading" }, spinner())));
    const text = (await commitTexts.get(path)) ?? "";
    const tokens = await texColours(path, text);
    if (!ctx.alive() || state.path !== path) return;
    state.text = text;
    state.colours = tokens;
    Object.assign(state, splitLines(state.text));
    paintHead();
    paintCode();
    codeScroll.scrollTop = 0;
  }

  // --- The comment box -------------------------------------------------------------------

  const lineOf = (i16) => {
    let lo = 0;
    let hi = state.starts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (state.starts[mid] <= i16) lo = mid;
      else hi = mid - 1;
    }
    return lo + 1;
  };

  function closeCompose() {
    state.compose = null;
    paintCode();
  }

  // sel is {lines: true, line_start, line_end} or {s16, e16}.
  function openCompose(sel) {
    const c = sel.lines
      ? { ...sel }
      : { ...sel, line_start: lineOf(sel.s16), line_end: lineOf(Math.max(sel.s16, sel.e16 - 1)) };
    const quote = c.lines
      ? state.lines.slice(c.line_start - 1, c.line_end).join("\n")
      : state.text.slice(c.s16, c.e16);
    let buttons;
    let note = null;
    const single = { text: "Add single comment", onClick: (v) => create(c, v, null) };
    if (ctx.finishing) {
      buttons = [{ text: "Add review comment", primary: true, onClick: (v) => create(c, v, review.id) }, single];
    } else if (!myPending) {
      buttons = [{ text: "Start a review", primary: true, onClick: (v) => startAndCreate(c, v) }, single];
    } else if (myPending.commit === commit) {
      buttons = [{ text: "Add to your review", primary: true, onClick: (v) => create(c, v, myPending.id) }, single];
    } else {
      buttons = [{ ...single, primary: true }];
      note = h("p", { class: "text-small color-muted mb-2" }, "Your pending review is on another state, so this can only be a single comment here. ",
        h("a", { href: `${ctx.base}/reviews/${enc(myPending.id)}/files`, text: "Open your review" }), ".");
    }
    const form = commentForm({ prefill: quote, buttons, onCancel: closeCompose, placeholder: "Leave a comment", label: "Comment" });
    c.quote = quote;
    c.el = h("div", { class: "box rv-compose" },
      h("div", { class: "rv-compose-head" },
        icon("comment", { size: 14, cls: "color-muted" }),
        h("strong", { text: `Comment on ${linesText(c.line_start, c.line_end)}` })),
      h("div", { class: "rv-compose-body" },
        c.lines ? null : h("blockquote", { class: "rv-compose-quote", text: quote.length > 400 ? `${quote.slice(0, 400)}…` : quote }),
        note,
        form.el));
    state.compose = c;
    paintCode();
    c.el.scrollIntoView({ block: "nearest" });
    form.focus();
    showInPdf(state.path, c.line_start);
  }

  async function create(c, v, reviewId) {
    const body = { path: state.path, ref: commit, body: v.body };
    if (c.lines) {
      body.line_start = c.line_start;
      body.line_end = c.line_end;
    } else {
      body.start = toCodePoints(state.text, c.s16);
      body.end = toCodePoints(state.text, c.e16);
    }
    if (v.suggestion !== null) body.suggestion = v.suggestion;
    if (reviewId) body.review_id = reviewId;
    const t = await api.post(projectUrl(ctx.ref, "/threads"), body);
    known.set(t.id, t);
    state.compose = null;
    countFiles();
    paintSelect();
    paintCode();
    ctx.setCounts([...known.values()]);
    refreshOpenCounts(p.id);
    if (!t.review_id) flash("Added your single comment. Everybody on the paper sees it now.", "success");
    return t;
  }

  async function startAndCreate(c, v) {
    const r = await api.post(projectUrl(ctx.ref, "/reviews"), { ref: commit });
    await create(c, v, r.id);
    queueFlash("Started your review. Your comments stay pending, and only you see them, until you finish it.", "success");
    navigate(`${ctx.base}/reviews/${enc(r.id)}/files?path=${enc(state.path)}`);
  }

  // --- Selecting text ---------------------------------------------------------------------

  function pointOffset(node, offset) {
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el || !table.contains(el)) return null;
    const lc = el.closest("td.lc");
    if (lc && lc.closest("tr.rv-line")) {
      const r = document.createRange();
      r.setStart(lc, 0);
      r.setEnd(node, offset);
      return Number(lc.dataset.off) + r.toString().length;
    }
    const tr = el.closest("tr.rv-line");
    return tr ? Number(tr.querySelector("td.lc").dataset.off) : null;
  }

  function codeSelection() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
    const r = sel.getRangeAt(0);
    const a = pointOffset(r.startContainer, r.startOffset);
    const b = pointOffset(r.endContainer, r.endOffset);
    if (a === null || b === null) return null;
    const s16 = Math.min(a, b);
    let e16 = Math.max(a, b);
    if (e16 > s16 && state.text[e16 - 1] === "\n") e16 -= 1;
    if (e16 <= s16) return null;
    const rects = r.getClientRects();
    return { s16, e16, rect: rects[rects.length - 1] || r.getBoundingClientRect() };
  }

  function hideFloat() {
    floatBtn.hidden = true;
    state.pending = null;
  }

  function showFloat() {
    if (!perms.comment) return;
    const sel = codeSelection();
    if (!sel) {
      hideFloat();
      return;
    }
    state.pending = sel;
    // It sits in the scrolling source, so it scrolls with the selection.
    const box = codeScroll.getBoundingClientRect();
    floatBtn.hidden = false;
    const width = floatBtn.offsetWidth || 100;
    const left = sel.rect.right - box.left + codeScroll.scrollLeft - width / 2;
    floatBtn.style.top = `${Math.round(sel.rect.bottom - box.top + codeScroll.scrollTop + 6)}px`;
    floatBtn.style.left = `${Math.round(Math.max(8, Math.min(left, codeScroll.scrollLeft + codeScroll.clientWidth - width - 8)))}px`;
  }

  floatBtn.addEventListener("mousedown", (e) => e.preventDefault());
  floatBtn.addEventListener("click", () => {
    const sel = state.pending;
    hideFloat();
    if (!sel) return;
    window.getSelection()?.removeAllRanges();
    openCompose({ s16: sel.s16, e16: sel.e16 });
  });
  table.addEventListener("mouseup", () => setTimeout(showFloat, 0));
  const onSelectionChange = debounce(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) hideFloat();
    else showFloat();
  }, 250);
  document.addEventListener("selectionchange", onSelectionChange);
  onLeave(() => {
    onSelectionChange.cancel();
    document.removeEventListener("selectionchange", onSelectionChange);
  });

  // Line numbers, and the marks of threads.
  table.addEventListener("mousedown", (e) => {
    if (e.shiftKey && e.target.closest("td.rv-ln")) e.preventDefault();
  });
  const lineClick = (e, ln) => {
    const n = Number(ln.dataset.ln);
    let a = n;
    let b = n;
    if (e.shiftKey && state.anchorLine) {
      a = Math.min(state.anchorLine, n);
      b = Math.max(state.anchorLine, n);
    } else {
      state.anchorLine = n;
    }
    window.getSelection()?.removeAllRanges();
    hideFloat();
    openCompose({ lines: true, line_start: a, line_end: b });
  };
  table.addEventListener("click", (e) => {
    const ln = e.target.closest("td.rv-ln");
    if (ln && perms.comment) {
      lineClick(e, ln);
      return;
    }
    const mark = e.target.closest("mark.rv-mark");
    if (mark && window.getSelection()?.isCollapsed) {
      const card = cards.get(mark.dataset.thread);
      if (card) {
        card.expand?.();
        card.scrollIntoView({ block: "nearest", behavior: "smooth" });
        selectThread(card.threadData(), card);
      }
    }
  });
  table.addEventListener("keydown", (e) => {
    const ln = e.target.closest?.("td.rv-ln");
    if (ln && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      lineClick(e, ln);
    }
  });

  function selectThread(t, card) {
    if (state.selected) cards.get(state.selected)?.classList.remove("is-selected");
    state.selected = t.id;
    card?.classList.add("is-selected");
    if (t.base.commit === commit) showInPdf(t.base.path, t.base.line_start);
  }

  // --- The PDF ------------------------------------------------------------------------------

  function setPdf(on) {
    pdf.on = on;
    store(PDF_KEY, on ? "on" : "off");
    pdfToggle.setAttribute("aria-pressed", String(on));
    split.classList.toggle("has-pdf", on);
    pdfCol.hidden = !on;
    hideFloat();
    if (!on) clearPick();
    if (on && !pdf.started) startPdf();
  }

  function pdfMessage(title, text, { iconName = "info", busy = false, action = null } = {}) {
    clear(pdfCol).append(h("div", { class: "box rv-pdf-message" },
      h("div", { class: "box-header box-header-sm" }, icon("pdf", { cls: "color-muted" }), h("strong", { class: "flex-1", text: "PDF" }),
        h("button", { type: "button", class: "btn-octicon", "aria-label": "Hide the PDF", title: "Hide the PDF", onclick: () => setPdf(false) }, icon("x"))),
      h("div", { class: "blankslate" }, busy ? spinner(true) : icon(iconName, { size: 24 }), h("h3", { text: title }), text ? h("p", { text }) : null, action)));
  }

  async function startPdf() {
    pdf.started = true;
    pdfMessage("Compiling this state", "The PDF shows what the reviewer read. The first compile of a state takes a while.", { busy: true });
    let c;
    try {
      c = await compileWait(ctx, "/compile", { ref: commit });
    } catch (err) {
      if (!ctx.alive()) return;
      if (err.status === 503) {
        // No compile service: the source takes the whole width, and the toggle says why.
        pdfToggle.disabled = true;
        pdfToggle.title = `No PDF here. ${err.message}`;
        pdf.on = false;
        pdfToggle.setAttribute("aria-pressed", "false");
        split.classList.remove("has-pdf");
        pdfCol.hidden = true;
      } else pdfMessage("The PDF could not be made", err.message, { iconName: "alert" });
      return;
    }
    if (!c || !ctx.alive()) return;
    if (!c.pdf_url) {
      const problems = (c.problems || []).filter((x) => x.level === "error");
      pdfMessage(c.status === "timeout" ? "Compiling took too long" : "LaTeX made no PDF",
        problems.length ? problems.slice(0, 2).map((x) => `${x.path ? `${x.path}:${x.line} ` : ""}${x.message}`).join(" ") : "The log says why.",
        { iconName: "alert", action: c.log_url ? h("a", { class: "btn", href: c.log_url, target: "_blank", rel: "noopener" }, "Open the log") : null });
      return;
    }
    pdf.compile = c;
    const hide = h("button", { type: "button", class: "btn-octicon", "aria-label": "Hide the PDF", title: "Hide the PDF", onclick: () => setPdf(false) }, icon("x"));
    pdf.view = pdfBox(pdfCol, {
      title: `PDF at ${commit.slice(0, 7)}${c.status === "errors" ? ", with LaTeX errors" : ""}`,
      url: c.pdf_url,
      name: `${p.name}-${commit.slice(0, 7)}.pdf`,
      onDoubleClick: fromPdf,
      onSelect: fromPdfSelection,
      extra: hide,
      cls: "rv-pdf",
    });
    pdfCol.append(pdfFloat);
  }

  // --- Text selected in the PDF ---------------------------------------------------------------

  // SyncTeX names the lines of the selection's start and end, and the passage
  // is looked for there (editor-spot.js). Where it is not found, its lines are
  // marked. The marks go as soon as the selection does.
  let pickToken = 0;

  async function fromPdfSelection(sel) {
    const mine = ++pickToken;
    if (!sel) {
      clearPick();
      return;
    }
    if (!pdf.compile) return;
    const ask = (point) => api.post(projectUrl(ctx.ref, `/compiles/${enc(pdf.compile.id)}/synctex/edit`), point)
      .catch((err) => {
        if (err.status === 404) return null;
        throw err;
      });
    let a;
    let b;
    try {
      [a, b] = await Promise.all([ask(sel.start), ask(sel.end)]);
    } catch (err) {
      if (mine === pickToken && ctx.alive()) flashError(err);
      return;
    }
    if (mine !== pickToken || !ctx.alive()) return;
    const at = a?.path ? a : b;
    if (!at?.path) {
      flash("SyncTeX knows no source line there. Select text of the paper's body.", "info");
      return;
    }
    if (!textFiles.some((f) => f.path === at.path)) {
      flash(`That text comes from ${at.path}, which is not a file of this state.`, "info");
      return;
    }
    if (at.path !== state.path) await loadFile(at.path);
    if (mine !== pickToken || !ctx.alive()) return;
    const first = at.line;
    const last = b?.path === at.path ? b.line : first;
    const found = findPassage(sourceDoc(), first, last, sel.text);
    state.pick = found
      ? { s16: found.from, e16: found.to }
      : { lines: true, line_start: Math.min(first, last), line_end: Math.max(first, last) };
    paintCode();
    table.querySelector(".rv-pick, tr.is-picked")?.scrollIntoView({ block: "center" });
    placePdfFloat();
  }

  // The marks go without painting the source again, which would undo a
  // selection someone is starting there.
  function clearPick() {
    pickToken += 1;
    state.pick = null;
    pdfFloat.hidden = true;
    table.querySelectorAll(".rv-pick").forEach((el) => el.classList.remove("rv-pick"));
    table.querySelectorAll("tr.is-picked").forEach((el) => el.classList.remove("is-picked"));
  }

  // The source as the doc findPassage reads, like CodeMirror's.
  function sourceDoc() {
    const line = (n) => ({ number: n, from: state.starts[n - 1], to: state.starts[n - 1] + state.lines[n - 1].length });
    return {
      lines: state.lines.length,
      line,
      lineAt: (pos) => line(lineOf(pos)),
      sliceString: (a, b) => state.text.slice(a, b),
    };
  }

  // The button sits under the selection's last line while it is in view.
  function placePdfFloat() {
    const sel = window.getSelection();
    const rects = sel?.rangeCount ? [...sel.getRangeAt(0).getClientRects()].filter((r) => r.width > 0 && r.height > 0) : [];
    const last = rects[rects.length - 1];
    const col = pdfCol.getBoundingClientRect();
    if (!perms.comment || !state.pick || !last || last.bottom < col.top + 48 || last.bottom > col.bottom - 40) {
      pdfFloat.hidden = true;
      return;
    }
    pdfFloat.hidden = false;
    const width = pdfFloat.offsetWidth || 100;
    pdfFloat.style.top = `${Math.round(last.bottom - col.top + 6)}px`;
    pdfFloat.style.left = `${Math.round(Math.max(8, Math.min(last.right - col.left - width / 2, col.width - width - 8)))}px`;
  }

  pdfCol.addEventListener("scroll", placePdfFloat, { capture: true, passive: true });
  pdfFloat.addEventListener("mousedown", (e) => e.preventDefault());
  pdfFloat.addEventListener("click", () => {
    const pick = state.pick;
    clearPick();
    window.getSelection()?.removeAllRanges();
    if (pick) openCompose(pick.lines ? { ...pick } : { s16: pick.s16, e16: pick.e16 });
  });

  async function fromPdf({ page, x, y }) {
    if (!pdf.compile) return;
    let at;
    try {
      at = await api.post(projectUrl(ctx.ref, `/compiles/${enc(pdf.compile.id)}/synctex/edit`), { page, x, y });
    } catch (err) {
      if (err.status === 404) flash("SyncTeX knows no source line there. Double-click on the text.", "info");
      else flashError(err);
      return;
    }
    if (!ctx.alive() || !at?.path) return;
    if (!textFiles.some((f) => f.path === at.path)) {
      flash(`That text comes from ${at.path}, which is not a file of this state.`, "info");
      return;
    }
    if (at.path !== state.path) await loadFile(at.path);
    if (!ctx.alive()) return;
    if (perms.comment) openCompose({ lines: true, line_start: at.line, line_end: at.line });
    else {
      const row = document.getElementById(`RL${at.line}`);
      row?.scrollIntoView({ block: "center" });
      row?.classList.add("is-flash");
      setTimeout(() => row?.classList.remove("is-flash"), 2000);
    }
  }

  function showInPdf(path, line) {
    if (!pdf.view || !pdf.compile) return;
    api.post(projectUrl(ctx.ref, `/compiles/${enc(pdf.compile.id)}/synctex/view`), { path, line, column: 0 })
      .then((res) => {
        if (ctx.alive() && pdf.view && res?.boxes?.length) pdf.view.showBoxes(res.boxes);
      })
      .catch(() => {});
  }

  onLeave(() => {
    pdf.view?.destroy();
    pdf.view = null;
  });

  // --- The layout -----------------------------------------------------------------------------

  // The source and the PDF fill the window below the review's header, like the
  // editor. On a phone they stack and the page scrolls.
  function fit() {
    if (!split.isConnected) return;
    if (window.matchMedia(PHONE).matches) {
      split.style.height = "";
      return;
    }
    const top = split.getBoundingClientRect().top + window.scrollY;
    const height = `${Math.max(320, Math.floor(window.innerHeight - top))}px`;
    if (split.style.height !== height) split.style.height = height;
  }
  window.addEventListener("resize", fit);
  // Flashes above the page and a wrapping header move it down.
  const resized = new ResizeObserver(fit);
  resized.observe(document.body);
  onLeave(() => {
    window.removeEventListener("resize", fit);
    resized.disconnect();
  });

  // The divider between the source and the PDF, as in the editor.
  const setWidth = (px) => {
    const both = codeCol.getBoundingClientRect().width + pdfCol.getBoundingClientRect().width;
    const width = Math.min(Math.max(240, px), both - 320);
    const share = width / split.clientWidth;
    pdfCol.style.flexBasis = `${share * 100}%`;
    store(PDF_WIDTH_KEY, String(share));
  };
  divider.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    divider.setPointerCapture(e.pointerId);
    split.classList.add("is-resizing");
    hideFloat();
    pdfFloat.hidden = true;
    const move = (m) => setWidth(split.getBoundingClientRect().right - m.clientX);
    const up = () => {
      split.classList.remove("is-resizing");
      divider.removeEventListener("pointermove", move);
      divider.removeEventListener("pointerup", up);
      divider.removeEventListener("pointercancel", up);
    };
    divider.addEventListener("pointermove", move);
    divider.addEventListener("pointerup", up);
    divider.addEventListener("pointercancel", up);
  });
  divider.addEventListener("dblclick", () => setWidth((codeCol.offsetWidth + pdfCol.offsetWidth) / 2));
  divider.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    setWidth(pdfCol.offsetWidth + (e.key === "ArrowLeft" ? 32 : -32));
  });

  // --- Go ---------------------------------------------------------------------------------------

  paintLater();
  clear(ctx.main).append(split);
  pdfCol.hidden = !pdf.on;
  split.classList.toggle("has-pdf", pdf.on);
  await loadFile(state.path);
  // The page goes up once this returns.
  requestAnimationFrame(fit);
  if (pdf.on) startPdf();
}
