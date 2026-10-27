// Comments in the browser editor (docs/DESIGN.md §13, In the web app).
//
// The open threads of the open file are highlighted in the text, from their
// start16/end16. CodeMirror moves the highlights along with every edit, and
// the server's positions replace them when the edits have settled and the
// threads are asked for again. A side panel, in the PDF's column, lists the
// file's threads with the review page's cards, so authors reply, resolve,
// apply suggestions, accept and reopen right next to the text. Selecting text
// offers "Comment". Threads of a pending review reach only their reviewer,
// because the server sends them to nobody else.
//
//   const comments = editorComments({ project, base, cm: () => cm, view: () => view,
//     path: () => openPath, reveal(from, to), openFile(path, line), show(on), note(text) });
//   extensions: comments.extension()   in every text file's EditorState
//   comments.opened(path | null)        another file is open
//   comments.edited()                   the document changed
//   comments.el, comments.button, comments.floatEl, comments.shown(on), comments.destroy()

import { api, enc, projectUrl, request } from "./api.js";
import { h, clear, plural } from "./dom.js";
import { icon } from "./icons.js";
import { session } from "./session.js";
import { counter, flash } from "./ui.js";
import {
  commentForm, linesText, permissions, refreshOpenCounts, threadCard, toCodePoints,
} from "./threads.js";

// The threads are asked for again this long after the last edit, and at least this often.
const SETTLE_MS = 2500;
const POLL_MS = 60000;
const NEAR = 400;

export function editorComments({ project, base, cm, view, path, reveal, openFile, show, note }) {
  const perms = permissions(project);
  const url = (rest) => projectUrl(project.id, rest);
  let threads = [];
  let reviews = [];
  let field = null;
  let setSpans = null;
  let selected = null;
  let shownNow = false;
  let compose = null; // {from, to, line_start, line_end, quote, el}
  let settleTimer = 0;
  let pollTimer = 0;
  let loading = null;
  let dead = false;
  let loaded = false;
  const cards = new Map();

  const env = {
    project,
    base,
    perms,
    texts: null,
    reviews: new Map(),
    onChange: (next, old) => {
      if (next) threads = threads.map((t) => (t.id === next.id ? next : t));
      else {
        threads = threads.filter((t) => t.id !== old.id);
        cards.delete(old.id);
      }
      refreshOpenCounts(project.id);
      paint();
      highlight();
    },
    onSelect: (t) => select(t.id, { scroll: true }),
  };

  // --- Panel ---------------------------------------------------------------------

  const count = counter("0");
  count.hidden = true;
  const button = h("button", {
    type: "button", class: "btn btn-sm editor-comments-btn", "aria-pressed": "false", "aria-label": "Comments",
    title: "Show the comments on this file",
  }, icon("comment"), h("span", { class: "hide-sm hide-narrow", text: "Comments" }), count);
  button.addEventListener("click", () => show(!shownNow));

  const list = h("div", { class: "editor-comments-list" });
  const composeHost = h("div");
  const body = h("div", { class: "editor-comments-body" }, composeHost, list);
  const el = h("section", { class: "editor-comments", "aria-label": "Comments" },
    h("div", { class: "editor-comments-head" },
      icon("comment", { cls: "color-muted" }),
      h("h2", { text: "Comments" }),
      h("span", { class: "flex-1" }),
      h("a", { class: "btn btn-sm", href: `${base}/reviews`, title: "Every conversation of the paper on the Reviews tab" }, "All"),
      h("button", { type: "button", class: "btn-octicon", "aria-label": "Hide the comments", title: "Hide the comments", onclick: () => show(false) }, icon("x"))),
    body);

  const floatEl = h("button", { type: "button", class: "btn btn-sm btn-primary editor-comment-float", hidden: true, title: "Comment on the selected text (Ctrl+Alt+M)" },
    icon("comment", { size: 14 }), "Comment");
  floatEl.addEventListener("mousedown", (e) => e.preventDefault());
  floatEl.addEventListener("click", () => startComment());

  const inFile = (t) => t.path === path();
  const highlighted = (t) => inFile(t) && !t.outdated && t.state !== "accepted";

  function cardFor(t) {
    let card = cards.get(t.id);
    if (!card) {
      card = threadCard(t, env, { snippet: t.outdated, pathLink: false, collapsed: t.state === "accepted" });
      cards.set(t.id, card);
    }
    return card;
  }

  function paint() {
    const here = threads.filter(inFile);
    const open = here.filter((t) => t.state === "open").length;
    count.textContent = String(open);
    count.hidden = !open;
    button.title = open ? `${plural(open, "open comment")} on this file` : "Show the comments on this file";
    if (!shownNow) return;
    clear(list);
    if (!path()) {
      list.append(h("p", { class: "editor-comments-empty", text: "Comments are on the text of a file. Open one." }));
      return;
    }
    if (!loaded) {
      list.append(h("p", { class: "editor-comments-empty", text: "Loading the comments…" }));
      return;
    }
    const active = here.filter((t) => t.state !== "accepted");
    const accepted = here.filter((t) => t.state === "accepted");
    if (!active.length && !compose) {
      list.append(h("p", { class: "editor-comments-empty", text: perms.comment
        ? "No open comments on this file. Select text and click Comment to start one."
        : "No open comments on this file." }));
    }
    list.append(...active.map(cardFor));
    if (accepted.length) {
      list.append(h("details", { class: "editor-comments-more" },
        h("summary", {}, `${plural(accepted.length, "accepted comment")}`),
        accepted.map(cardFor)));
    }
    const others = new Map();
    for (const t of threads) if (!inFile(t) && t.state === "open") others.set(t.path, (others.get(t.path) || 0) + 1);
    if (others.size) {
      list.append(h("div", { class: "editor-comments-others" },
        h("h3", { text: "Open comments on other files" }),
        h("ul", {}, [...others].sort((a, b) => a[0].localeCompare(b[0])).map(([p, n]) => h("li", {},
          h("button", { type: "button", class: "btn-link text-mono", text: p, onclick: () => openFile(p, firstLine(p)) }),
          h("span", { class: "color-muted", text: ` · ${n}` }))))));
    }
    if (selected) cards.get(selected)?.classList.add("is-selected");
  }

  function firstLine(p) {
    return threads.find((t) => t.path === p && t.state === "open")?.line_start || 1;
  }

  // --- Highlights in the text ---------------------------------------------------------

  function makeField(lib) {
    setSpans = lib.StateEffect.define();
    const build = (spans) => lib.Decoration.set(spans.map((s) => {
      const cls = ["cm-rv-thread", `is-${s.state}`, s.pending && "is-pending", s.id === selected && "is-selected"].filter(Boolean).join(" ");
      return lib.Decoration.mark({ class: cls, attributes: { "data-thread": s.id, title: s.title } }).range(s.from, s.to);
    }), true);
    field = lib.StateField.define({
      create: (state) => {
        const spans = spansFor(state.doc, []);
        return { spans, deco: build(spans) };
      },
      update: (value, tr) => {
        for (const e of tr.effects) {
          if (e.is(setSpans)) return { spans: e.value, deco: build(e.value) };
        }
        if (!tr.docChanged) return value;
        // Text typed at a span's start stays outside it, text at its end joins it, as on the server.
        const spans = value.spans
          .map((s) => ({ ...s, from: tr.changes.mapPos(s.from, 1), to: tr.changes.mapPos(s.to, 1) }))
          .filter((s) => s.to > s.from);
        return { spans, deco: build(spans) };
      },
      provide: (f) => lib.EditorView.decorations.from(f, (v) => v.deco),
    });
  }

  // Where each thread is in `doc`: the server's place when its text is there,
  // else where the edits since moved it, else the text found nearby.
  function spansFor(doc, current) {
    const before = new Map(current.map((s) => [s.id, s]));
    const out = [];
    for (const t of threads) {
      if (!highlighted(t)) continue;
      let from = t.start16;
      let to = t.end16;
      if (!(to <= doc.length && from < to && doc.sliceString(from, to) === t.text)) {
        const old = before.get(t.id);
        if (old) ({ from, to } = old);
        else {
          if (!t.text) continue;
          const lo = Math.max(0, from - NEAR);
          const at = doc.sliceString(lo, Math.min(doc.length, from + t.text.length + NEAR)).indexOf(t.text);
          if (at < 0) continue;
          from = lo + at;
          to = from + t.text.length;
        }
      }
      const first = t.comments.find((c) => c.kind === "comment");
      const words = first?.body || (first?.suggestion !== null ? "Suggested a change" : "");
      out.push({
        id: t.id, from, to, state: t.state, pending: t.pending,
        title: `${first?.author?.name || first?.author?.email || "Someone"}: ${words.length > 120 ? `${words.slice(0, 120)}…` : words}`,
      });
    }
    return out;
  }

  function highlight() {
    const v = view();
    if (!v || !field) return;
    const current = v.state.field(field, false);
    if (!current) return;
    v.dispatch({ effects: setSpans.of(spansFor(v.state.doc, current.spans)) });
  }

  function spanOf(id) {
    const v = view();
    return v && field ? v.state.field(field, false)?.spans.find((s) => s.id === id) || null : null;
  }

  function select(id, { scroll = false, fromText = false } = {}) {
    if (selected) cards.get(selected)?.classList.remove("is-selected");
    selected = id;
    const card = cards.get(id);
    card?.classList.add("is-selected");
    if (fromText) card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    highlight();
    const span = spanOf(id);
    if (scroll && span) reveal(span.from, span.to);
  }

  function extension() {
    const lib = cm();
    if (!field) makeField(lib);
    return [
      field,
      lib.EditorView.domEventHandlers({
        click: (e) => {
          const mark = e.target.closest?.(".cm-rv-thread");
          if (!mark || !shownNow) return false;
          select(mark.dataset.thread, { fromText: true });
          return false;
        },
      }),
      lib.EditorView.updateListener.of((u) => {
        if (u.docChanged) mapCompose(u.changes);
        if (u.selectionSet || u.focusChanged || u.geometryChanged || u.viewportChanged) placeFloat(u.view);
      }),
      lib.keymap.of([{ key: "Mod-Alt-m", run: () => startComment() }]),
    ];
  }

  // The "Comment" button next to a selection.
  function placeFloat(v) {
    const sel = v.state.selection.main;
    if (!perms.comment || sel.empty || !v.hasFocus || !path()) {
      floatEl.hidden = true;
      return;
    }
    v.requestMeasure({
      read: () => {
        const at = v.coordsAtPos(sel.head, sel.head > sel.anchor ? -1 : 1);
        const host = floatEl.parentElement?.getBoundingClientRect();
        const scroller = v.scrollDOM.getBoundingClientRect();
        return at && host ? { at, host, scroller } : null;
      },
      write: (m) => {
        if (!m || m.at.bottom < m.scroller.top || m.at.top > m.scroller.bottom) {
          floatEl.hidden = true;
          return;
        }
        floatEl.hidden = false;
        const width = floatEl.offsetWidth || 100;
        floatEl.style.top = `${Math.round(Math.min(m.at.bottom - m.host.top + 6, m.host.height - 36))}px`;
        floatEl.style.left = `${Math.round(Math.max(8, Math.min(m.at.left - m.host.left, m.host.width - width - 8)))}px`;
      },
    });
  }

  // --- Writing a comment -------------------------------------------------------------

  function myPending() {
    return reviews.find((r) => r.state === "pending" && r.reviewer?.id === session.me?.id) || null;
  }

  function startComment() {
    const v = view();
    if (!v || !perms.comment || !path()) return false;
    const sel = v.state.selection.main;
    if (sel.empty) {
      note("Select the text you want to comment on.");
      return true;
    }
    floatEl.hidden = true;
    const doc = v.state.doc;
    const from = sel.from;
    let to = sel.to;
    if (to > from && doc.sliceString(to - 1, to) === "\n") to -= 1;
    const quote = doc.sliceString(from, to);
    compose = {
      from, to, quote, file: path(),
      line_start: doc.lineAt(from).number,
      line_end: doc.lineAt(Math.max(from, to - 1)).number,
    };
    show(true);
    paintCompose();
    return true;
  }

  function paintCompose() {
    clear(composeHost);
    if (!compose) return;
    const c = compose;
    const pending = myPending();
    const single = { text: "Add single comment", onClick: (v) => create(c, v, null) };
    const buttons = pending
      ? [{ text: "Add to your review", primary: true, onClick: (v) => create(c, v, pending.id) }, single]
      : [{ ...single, primary: true }, { text: "Start a review", onClick: (v) => startReview(c, v) }];
    const form = commentForm({
      prefill: c.quote,
      buttons,
      onCancel: () => {
        compose = null;
        paintCompose();
        paint();
        view()?.focus();
      },
      label: "Comment",
    });
    composeHost.append(h("div", { class: "box rv-compose editor-compose" },
      h("div", { class: "rv-compose-head" },
        icon("comment", { size: 14, cls: "color-muted" }),
        h("strong", { text: `Comment on ${linesText(c.line_start, c.line_end)}` })),
      h("div", { class: "rv-compose-body" },
        h("blockquote", { class: "rv-compose-quote", text: c.quote.length > 400 ? `${c.quote.slice(0, 400)}…` : c.quote }),
        pending ? h("p", { class: "text-small color-muted mb-2", text: "Comments added to your review stay pending, and only you see them, until you finish it." }) : null,
        form.el)));
    form.focus();
  }

  async function create(c, values, reviewId) {
    const v = view();
    if (!v || path() !== c.file) throw new Error("The file changed. Select the text again.");
    // The selection's place now, after the edits made while the comment was written.
    const span = c.mapped || { from: c.from, to: c.to };
    const text = v.state.doc.toString();
    const body = {
      path: c.file, ref: "live",
      start: toCodePoints(text, span.from), end: toCodePoints(text, span.to),
      body: values.body,
    };
    if (values.suggestion !== null) body.suggestion = values.suggestion;
    if (reviewId) body.review_id = reviewId;
    const t = await api.post(url("/threads"), body);
    compose = null;
    paintCompose();
    threads = [...threads, t];
    loaded = true;
    refreshOpenCounts(project.id);
    if (!t.review_id) flash("Added your comment. Everybody on the paper sees it now.", "success");
    paint();
    select(t.id);
    return t;
  }

  async function startReview(c, values) {
    const res = await request("POST", url("/reviews"), { body: { ref: "live" }, raw: true });
    const review = await res.json();
    reviews = [...reviews.filter((r) => r.id !== review.id), review];
    env.reviews.set(review.id, review);
    await create(c, values, review.id);
    flash(h("span", {}, "Started your review on the text as it is now. ",
      h("a", { href: `${base}/reviews/${enc(review.id)}`, text: "Finish it" }), " when you are done."), "success");
  }

  // Edits while a comment is written move its span along.
  function mapCompose(changes) {
    if (!compose || compose.file !== path()) return;
    const from = compose.mapped?.from ?? compose.from;
    const to = compose.mapped?.to ?? compose.to;
    compose.mapped = { from: changes.mapPos(from, 1), to: changes.mapPos(to, 1) };
  }

  // What a card shows. Positions move with every edit and are left out.
  const looks = (t) => JSON.stringify([t.state, t.pending, t.text, t.outdated, t.changed, t.resolution?.at, t.accepted?.at,
    t.comments.map((c) => [c.id, c.edited_at])]);

  // --- Loading -----------------------------------------------------------------------

  async function load() {
    if (dead) return;
    if (loading) return loading;
    loading = (async () => {
      try {
        const [list, revs] = await Promise.all([
          api.get(url("/threads"), { query: { state: "all" } }),
          api.get(url("/reviews")),
        ]);
        if (dead) return;
        threads = list;
        reviews = revs;
        env.reviews = new Map(revs.map((r) => [r.id, r]));
        // Cards of threads that changed are drawn again, unless something is being written in them.
        for (const t of threads) {
          const card = cards.get(t.id);
          if (card && !card.querySelector(".rv-form") && looks(card.threadData()) !== looks(t)) card.update(t);
        }
        loaded = true;
        paint();
        highlight();
      } catch (err) {
        if (err?.status !== 401) console.warn("The comments could not be loaded", err);
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  function poll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => {
      if (document.visibilityState !== "hidden") load();
      poll();
    }, POLL_MS);
  }

  const onVisible = () => {
    if (document.visibilityState === "visible") load();
  };
  document.addEventListener("visibilitychange", onVisible);

  return {
    el,
    button,
    floatEl,
    extension,
    start() {
      load();
      poll();
    },
    opened() {
      floatEl.hidden = true;
      if (compose && compose.file !== path()) {
        compose = null;
        paintCompose();
      }
      paint();
    },
    edited() {
      clearTimeout(settleTimer);
      settleTimer = setTimeout(load, SETTLE_MS);
    },
    shown(on) {
      shownNow = on;
      button.setAttribute("aria-pressed", String(on));
      paint();
      paintCompose();
      if (on && !loaded) load();
    },
    get isShown() {
      return shownNow;
    },
    destroy() {
      dead = true;
      clearTimeout(settleTimer);
      clearTimeout(pollTimer);
      document.removeEventListener("visibilitychange", onVisible);
    },
  };
}

