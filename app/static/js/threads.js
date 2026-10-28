// Review threads, drawn like GitHub's review comments (docs/DESIGN.md §13).
//
// A thread card shows where a comment is (the file and its current lines), the
// text it is on with a few lines around it, the conversation with its events,
// suggestions as small diffs, how the text changed when it was resolved, and
// the buttons the reader may use. The Reviews tab, the review page and the
// editor's comment panel all use the same card.
//
// The server says what each person may do (§13, Lifecycle and roles). The
// card hides what the reader's role does not allow, and shows the server's
// refusal in a flash when it says no anyway.

import { api, enc, encPath, projectUrl, request } from "./api.js";
import { h, clear, nextId, plural } from "./dom.js";
import { icon } from "./icons.js";
import { renderMarkdown } from "./markdown.js";
import { attachMentions } from "./mentions.js";
import { session } from "./session.js";
import { timeAgo } from "./time.js";
import { avatar, busy, confirmDialog, counter, dropdown, flash, flashError, openDialog, personName } from "./ui.js";

// --- Positions --------------------------------------------------------------------

// The API counts in Unicode code points, JavaScript strings in UTF-16 units.
export function toCodePoints(text, i16) {
  return Array.from(text.slice(0, i16)).length;
}

export function toUtf16(text, cp) {
  let i16 = 0;
  let n = 0;
  for (const ch of text) {
    if (n >= cp) break;
    i16 += ch.length;
    n += 1;
  }
  return i16;
}

export function linesText(first, last) {
  return first === last ? `line ${first}` : `lines ${first} to ${last}`;
}

// --- Roles ------------------------------------------------------------------------

export function permissions(project) {
  const role = project?.role || "viewer";
  return {
    role,
    comment: role === "owner" || role === "editor" || role === "reviewer",
    resolve: role === "owner" || role === "editor",
    owner: role === "owner",
  };
}

function isMe(person) {
  return Boolean(person && session.me && person.id === session.me.id);
}

function hasSuggestion(thread) {
  return thread.comments.some((c) => c.kind === "comment" && c.suggestion !== null && c.suggestion !== undefined);
}

// What the reader may do with a thread right now. An open thread can only be
// accepted when it carries a suggestion, a resolved one always.
export function threadActions(thread, perms) {
  const live = !thread.pending;
  return {
    reply: perms.comment,
    resolve: perms.resolve && live && thread.state === "open",
    apply: perms.resolve && live && thread.state === "open" && !thread.outdated,
    accept: perms.comment && live && (thread.state === "resolved" || (thread.state === "open" && hasSuggestion(thread)))
      && (isMe(thread.created_by) || perms.owner),
    reopen: perms.comment && live && (thread.state === "resolved" || thread.state === "accepted"),
  };
}

// --- Labels -----------------------------------------------------------------------

export const VERDICTS = {
  approve: { text: "Approved", cls: "label-success", icon: "check-circle", color: "color-success", says: "approved this paper" },
  changes_requested: { text: "Changes requested", cls: "label-danger", icon: "x-circle", color: "color-danger", says: "requested changes" },
  comment: { text: "Commented", cls: "label-secondary", icon: "comment", color: "color-muted", says: "reviewed" },
};

export function verdictLabel(review) {
  if (review.state === "pending") return h("span", { class: "Label label-attention", text: "Pending, only you see it" });
  const v = VERDICTS[review.verdict] || VERDICTS.comment;
  return h("span", { class: `Label ${v.cls}`, text: v.text });
}

export function verdictIcon(review, size = 16) {
  if (review.state === "pending") return icon("clock", { size, cls: "color-attention", label: "Pending" });
  const v = VERDICTS[review.verdict] || VERDICTS.comment;
  return icon(v.icon, { size, cls: v.color, label: v.text });
}

const STATES = {
  open: ["Open", "label-success"],
  resolved: ["Resolved", "label-done"],
  accepted: ["Accepted", "label-secondary"],
};

export function stateLabel(thread) {
  const [text, cls] = STATES[thread.state] || [thread.state, "label-secondary"];
  return h("span", { class: `Label ${cls}`, text });
}

// "Submission (v1-submission)" or "commit abc1234", for a review's base.
export function reviewBase(review, base) {
  const commit = review.commit || "";
  if (review.version) {
    return h("span", {},
      h("strong", { text: review.version.label || review.version.tag }), " ",
      h("a", { class: "tag-pill", href: `${base}/versions#v-${enc(review.version.tag)}`, title: "The version this review is on" },
        icon("tag", { size: 12 }), review.version.tag));
  }
  return h("span", {}, "commit ", h("a", { class: "sha", href: `${base}/commit/${enc(commit)}`, text: commit.slice(0, 7) }));
}

export function reviewTitle(review) {
  return `Review by ${personName(review.reviewer)}`;
}

// --- Text of files ----------------------------------------------------------------

// File contents at one ref, each fetched once.
export class FileTexts {
  constructor(projectId, ref = "live") {
    this.projectId = projectId;
    this.ref = ref;
    this.cache = new Map();
  }

  get(path) {
    if (!this.cache.has(path)) {
      const p = request("GET", projectUrl(this.projectId, `/files/${encPath(path)}`), { query: { ref: this.ref }, raw: true })
        .then((res) => res.text())
        .catch(() => null);
      this.cache.set(path, p);
    }
    return this.cache.get(path);
  }

  forget(path = null) {
    if (path) this.cache.delete(path);
    else this.cache.clear();
  }
}

// --- Word diffs -------------------------------------------------------------------

const TOKEN = /\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu;
const MAX_CELLS = 250000;

// A word diff of two strings as [[op, text]], op being " ", "-" or "+".
// Changes next to each other, and the blanks between them, are joined, so a
// rewritten phrase reads as one deletion and one insertion.
export function diffWords(a, b) {
  const ta = String(a).match(TOKEN) || [];
  const tb = String(b).match(TOKEN) || [];
  let pre = 0;
  while (pre < ta.length && pre < tb.length && ta[pre] === tb[pre]) pre += 1;
  let suf = 0;
  while (suf < ta.length - pre && suf < tb.length - pre && ta[ta.length - 1 - suf] === tb[tb.length - 1 - suf]) suf += 1;
  const A = ta.slice(pre, ta.length - suf);
  const B = tb.slice(pre, tb.length - suf);
  const raw = [];
  if (A.length * B.length > MAX_CELLS) {
    A.forEach((t) => raw.push(["-", t]));
    B.forEach((t) => raw.push(["+", t]));
  } else {
    const n = A.length;
    const m = B.length;
    const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { raw.push([" ", A[i]]); i += 1; j += 1; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { raw.push(["-", A[i]]); i += 1; }
      else { raw.push(["+", B[j]]); j += 1; }
    }
    while (i < n) raw.push(["-", A[i++]]);
    while (j < m) raw.push(["+", B[j++]]);
  }
  // Blanks between two changes become part of both sides.
  for (let k = 1; k < raw.length - 1; k++) {
    if (raw[k][0] === " " && /^[ \t]+$/.test(raw[k][1]) && raw[k - 1][0] !== " " && raw[k + 1][0] !== " ") {
      raw.splice(k, 1, ["-", raw[k][1]], ["+", raw[k][1]]);
    }
  }
  const out = [];
  const push = (op, text) => {
    if (!text) return;
    const last = out[out.length - 1];
    if (last && last[0] === op) last[1] += text;
    else out.push([op, text]);
  };
  push(" ", ta.slice(0, pre).join(""));
  let del = "";
  let ins = "";
  for (const [op, t] of raw) {
    if (op === "-") del += t;
    else if (op === "+") ins += t;
    else {
      push("-", del);
      push("+", ins);
      del = "";
      ins = "";
      push(" ", t);
    }
  }
  push("-", del);
  push("+", ins);
  push(" ", ta.slice(ta.length - suf).join(""));
  return out;
}

// Diff rows like GitHub's suggested changes: the old lines in red with the
// removed words marked, then the new lines in green with the added words
// marked. Lines that did not change are shown once. `ops` is diffWords()'s.
function diffRows(ops, oldLine, newLine) {
  const rows = [];
  let o = oldLine;
  let n = newLine;
  let chunk = [];
  const row = (cls, a, b, content) => h("tr", { class: cls },
    h("td", { class: "num", dataset: { n: a ?? "" } }),
    h("td", { class: "num", dataset: { n: b ?? "" } }),
    h("td", { class: "code" }, content));
  // The segments of one side, split into lines, its own changes marked.
  const side = (segs, mark, tag) => {
    const lines = [[]];
    for (const [op, text] of segs) {
      if (text === "\n") lines.push([]);
      else lines[lines.length - 1].push(op === mark ? h(tag, { text }) : text);
    }
    return lines;
  };
  const flush = () => {
    if (!chunk.length) return;
    const changed = chunk.some(([op]) => op !== " ");
    const olds = chunk.filter(([op]) => op !== "+");
    const news = chunk.filter(([op]) => op !== "-");
    if (!changed) rows.push(row("ctx", o++, n++, olds.map(([, t]) => t)));
    else {
      if (olds.some(([, t]) => t !== "\n" && t)) side(olds, "-", "del").forEach((c) => rows.push(row("del", o++, null, c)));
      if (news.some(([, t]) => t !== "\n" && t)) side(news, "+", "ins").forEach((c) => rows.push(row("add", null, n++, c)));
    }
    chunk = [];
  };
  for (const [op, text] of ops) {
    text.split("\n").forEach((part, k) => {
      if (k > 0) {
        if (op === " ") flush();
        else chunk.push([op, "\n"]);
      }
      if (part) chunk.push([op, part]);
    });
  }
  flush();
  return rows;
}

function diffTable(rows) {
  return h("div", { class: "rv-diff" }, h("table", { class: "diff-table" }, h("tbody", {}, rows)));
}

export function wordDiffTable(oldText, newText, { oldLine = 1, newLine = 1 } = {}) {
  return diffTable(diffRows(diffWords(oldText, newText), oldLine, newLine));
}

// A stored resolution diff, git's --word-diff=porcelain with the file's line
// numbers. git writes the new side exactly but not always the old one's
// blanks, so the new text is taken from it and compared with the quote, which
// is the old text exactly.
export function storedDiffTable(diff, quote) {
  const ops = [];
  let oldLine = 1;
  let newLine = 1;
  for (const line of String(diff || "").split("\n")) {
    const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (m) {
      oldLine = Number(m[1]);
      newLine = Number(m[2]);
      continue;
    }
    if (line === "~") ops.push([" ", "\n"]);
    else if (line[0] === " " || line[0] === "-" || line[0] === "+") ops.push([line[0], line.slice(1)]);
  }
  // git ends the last line with "~", which is no line of its own.
  while (ops.length && ops[ops.length - 1][0] === " " && ops[ops.length - 1][1] === "\n") ops.pop();
  if (!ops.length) return null;
  if (typeof quote === "string") {
    const after = ops.filter(([op]) => op !== "-").map(([, t]) => t).join("");
    if (after === quote) return null;
    return wordDiffTable(quote, after, { oldLine, newLine });
  }
  return diffTable(diffRows(ops, oldLine, newLine));
}

// --- Snippets ---------------------------------------------------------------------

// The lines of `text` around [s16, e16), the span marked. `firstLine` is the
// expected number of the span's first line, to check that the text still fits.
// Only the span is yellow. The lines it is on get a bar in the gutter, since a
// LaTeX paragraph is often one line and a yellow line would be the paragraph.
export function snippetTable(text, s16, e16, { context = 2, firstLine = null, cls = "" } = {}) {
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const starts = [];
  let off = 0;
  for (const l of lines) {
    starts.push(off);
    off += l.length + 1;
  }
  const lineAt = (i) => {
    let lo = 0;
    let hi = starts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (starts[mid] <= i) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  };
  const first = lineAt(s16);
  // The span's last character. A span that ends with a newline ends on its line.
  const last = Math.max(first, lineAt(e16 > s16 ? e16 - 1 : s16));
  if (firstLine !== null && first + 1 !== firstLine) return null;
  const from = Math.max(0, first - context);
  const to = Math.min(lines.length - 1, last + context);
  const rows = [];
  for (let i = from; i <= to; i++) {
    const ls = starts[i];
    const le = ls + lines[i].length;
    const a = Math.max(ls, s16);
    const b = Math.min(le, e16);
    const inSpan = i >= first && i <= last;
    const content = a < b
      ? [lines[i].slice(0, a - ls), h("mark", { class: "rv-hl", text: lines[i].slice(a - ls, b - ls) }), lines[i].slice(b - ls)]
      : lines[i];
    rows.push(h("tr", { class: inSpan ? "is-marked" : null },
      h("td", { class: "ln", dataset: { ln: i + 1 } }),
      h("td", { class: "lc" }, content)));
  }
  return h("div", { class: `rv-snippet ${cls}`.trim() }, h("table", { class: "code-table" }, h("tbody", {}, rows)));
}

function quoteTable(quote, firstLine) {
  const lines = quote.split("\n");
  return h("div", { class: "rv-snippet is-outdated" }, h("table", { class: "code-table" }, h("tbody", {},
    lines.map((l, i) => h("tr", { class: "is-span" },
      h("td", { class: "ln", dataset: { ln: firstLine + i } }),
      h("td", { class: "lc", text: l }))))));
}

// --- Forms ------------------------------------------------------------------------

// A comment box like GitHub's: a text area, a "Suggest a change" toggle that
// opens an editable copy of the text, and the buttons. Each button is
// {text, primary, onClick(values, button)}. values is {body, suggestion},
// suggestion being null without one. Ctrl/Cmd+Enter presses the first button.
// With projectId, typing @ completes the paper's members (mentions.js).
export function commentForm({
  body = "", suggestion = null, prefill = "", allowSuggestion = true, placeholder = "Leave a comment",
  buttons, onCancel = null, cancelText = "Cancel", label = "Comment", projectId = null,
}) {
  const ta = h("textarea", { class: "form-control input-block rv-textarea", rows: "4", placeholder, "aria-label": label });
  ta.value = body;
  attachMentions(ta, projectId);
  const sugg = h("textarea", { class: "form-control input-block input-mono rv-suggest-text", rows: "3", "aria-label": "Suggested text", spellcheck: "false" });
  let suggesting = suggestion !== null;
  sugg.value = suggesting ? suggestion : prefill;
  const suggBox = h("div", { class: "rv-suggest", hidden: !suggesting },
    h("div", { class: "rv-suggest-head" }, icon("pencil", { size: 14 }), "Suggested change", h("span", { class: "color-muted text-normal", text: " · replaces the text you commented on" })),
    sugg);
  const toggle = allowSuggestion
    ? h("button", { type: "button", class: "btn btn-sm rv-suggest-toggle", "aria-pressed": String(suggesting), title: "Propose new text that the authors can apply with one click" },
      icon("pencil", { size: 14 }), "Suggest a change")
    : null;
  toggle?.addEventListener("click", () => {
    suggesting = !suggesting;
    toggle.setAttribute("aria-pressed", String(suggesting));
    suggBox.hidden = !suggesting;
    if (suggesting) {
      sugg.focus();
      sugg.setSelectionRange(0, sugg.value.length);
    } else ta.focus();
  });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const values = () => ({ body: ta.value.trim(), suggestion: suggesting ? sugg.value : null });
  const btnEls = buttons.map((b) => {
    const el = h("button", { type: "button", class: `btn btn-sm${b.primary ? " btn-primary" : ""}`, text: b.text });
    el.addEventListener("click", async () => {
      const v = values();
      if (!v.body && v.suggestion === null) {
        error.textContent = "Write a comment or suggest a change.";
        error.hidden = false;
        ta.focus();
        return;
      }
      error.hidden = true;
      try {
        await busy(el, () => b.onClick(v, el));
      } catch (err) {
        if (err?.status === 401) return;
        error.textContent = err.message || String(err);
        error.hidden = false;
      }
    });
    return el;
  });
  const keys = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      btnEls[0]?.click();
    }
    if (e.key === "Escape" && onCancel) {
      e.preventDefault();
      onCancel();
    }
  };
  ta.addEventListener("keydown", keys);
  sugg.addEventListener("keydown", keys);
  const el = h("div", { class: "rv-form" },
    ta,
    suggBox,
    error,
    h("div", { class: "rv-form-actions" },
      toggle,
      h("span", { class: "flex-1" }),
      onCancel ? h("button", { type: "button", class: "btn btn-sm", text: cancelText, onclick: onCancel }) : null,
      // Secondary buttons first, the primary one last, as on GitHub.
      [...btnEls].reverse()));
  return { el, focus: () => ta.focus(), values };
}

// A dialog with an optional note. Resolves with the note, or null when cancelled.
export function noteDialog({ title, text, confirmText, placeholder = "", danger = false, projectId = null }) {
  return new Promise((resolve) => {
    let result = null;
    const ta = h("textarea", { class: "form-control input-block", rows: "4", placeholder, id: nextId("note") });
    attachMentions(ta, projectId);
    const ok = h("button", { type: "button", class: `btn ${danger ? "btn-danger" : "btn-primary"}`, text: confirmText });
    const d = openDialog({
      title,
      body: [text ? h("p", { text }) : null,
        h("label", { class: "form-label", for: ta.id }, "Note", h("span", { class: "optional", text: " (optional)" })),
        ta],
      footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), ok],
      onClose: () => resolve(result),
    });
    ok.addEventListener("click", () => {
      result = ta.value.trim();
      d.close();
    });
    ta.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") ok.click();
    });
    ta.focus();
  });
}

// --- The card ---------------------------------------------------------------------

const EVENTS = {
  resolved: ["check", "resolved this conversation", "is-resolved"],
  applied: ["check", "applied the suggestion", "is-resolved"],
  accepted: ["check-circle", "accepted the change", "is-accepted"],
  reopened: ["history", "reopened this conversation", "is-reopened"],
};
const APPLIED_NOTE = "Applied the suggestion";

// env: {
//   project, base ("/p/<name>"), perms (permissions(project)),
//   texts      a FileTexts of live, for the snippets
//   reviews    Map of review id to review, to name a thread's review
//   onChange(thread, old)   the server sent a changed thread (null when it went)
//   onSelect(thread)        optional, a click on the card
// }
// opts: {
//   snippet      show the text with lines around it (default true)
//   baseLines    name the base's lines in the header, for the review's Files view
//   reviewCommit mark threads whose base is another commit
//   showReview   name the review the thread is in (default true)
//   pathLink     link the path to the Files tab (default true)
//   headExtra(thread)  more nodes for the header
// }
export function threadCard(thread, env, opts = {}) {
  const card = h("section", { class: "box rv-thread", id: `thread-${thread.id}`, dataset: { thread: thread.id } });
  let current = thread;
  let collapsed = opts.collapsed ?? thread.state === "accepted";
  let replyOpen = false;

  const url = (rest) => projectUrl(env.project.id, rest);

  async function act(promise, message = null) {
    try {
      const next = await promise;
      if (message) flash(message, "success");
      update(next);
      return next;
    } catch (err) {
      flashError(err);
      return null;
    }
  }

  function update(next) {
    const old = current;
    if (!next) {
      card.remove();
      env.texts?.forget(old.path);
      env.onChange?.(null, old);
      return;
    }
    current = next;
    if (next.state === "accepted" && old.state !== "accepted") collapsed = true;
    if (next.state !== "accepted" && old.state === "accepted") collapsed = false;
    env.texts?.forget(next.path);
    paint();
    env.onChange?.(next, old);
  }

  function header() {
    const t = current;
    const usesBase = opts.baseLines || t.outdated;
    const path = usesBase ? t.base.path : t.path;
    const first = usesBase ? t.base.line_start : t.line_start;
    const last = usesBase ? t.base.line_end : t.line_end;
    const toggle = h("button", {
      type: "button", class: "btn-octicon rv-thread-toggle", "aria-expanded": String(!collapsed),
      "aria-label": collapsed ? "Show the conversation" : "Hide the conversation",
    }, icon(collapsed ? "chevron-right" : "chevron-down"));
    toggle.addEventListener("click", () => {
      collapsed = !collapsed;
      paint();
    });
    const pathEl = t.outdated || opts.baseLines || opts.pathLink === false
      ? h("span", { class: "rv-path", text: path })
      : h("a", { class: "rv-path", href: `${env.base}/files/${encPath(path)}#L${first}`, title: "Open the live file at this line", text: path });
    const review = t.review_id ? env.reviews?.get(t.review_id) : null;
    let origin = null;
    if (opts.showReview !== false) {
      if (!t.review_id) origin = h("span", { class: "text-small color-muted nowrap", text: "Single comment" });
      else if (review) {
        origin = h("a", { class: "text-small nowrap", href: `${env.base}/reviews/${enc(t.review_id)}#thread-${t.id}` }, `in ${personName(review.reviewer)}'s review`);
      } else {
        origin = h("a", { class: "text-small nowrap", href: `${env.base}/reviews/${enc(t.review_id)}#thread-${t.id}`, text: "in a review" });
      }
    }
    const later = opts.reviewCommit && t.base.commit !== opts.reviewCommit;
    return h("div", { class: "rv-thread-head" },
      toggle,
      icon("file-text", { cls: "color-muted" }),
      h("div", { class: "rv-where" },
        pathEl,
        h("span", { class: "rv-lines", text: linesText(first, last) }),
        opts.baseLines && !t.outdated && (t.path !== t.base.path || t.line_start !== t.base.line_start)
          ? h("span", { class: "text-small color-muted", text: `now ${t.path !== t.base.path ? `${t.path}, ` : ""}${linesText(t.line_start, t.line_end)}` }) : null),
      h("div", { class: "rv-labels" },
        t.pending ? h("span", { class: "Label label-attention", title: "Only you see it until you finish your review", text: "Pending" }) : null,
        t.pending ? null : stateLabel(t),
        t.outdated ? h("span", { class: "Label label-attention", title: "The text this comment was on is gone", text: "Outdated" }) : null,
        !t.outdated && t.changed ? h("span", { class: "Label label-accent", title: "The text changed since the comment was made", text: "Changed" }) : null,
        later ? h("span", { class: "Label", title: "Made on the live text after this review started", text: "Made on a later state" }) : null),
      h("span", { class: "flex-1" }),
      opts.headExtra ? opts.headExtra(t) : null,
      origin);
  }

  function snippet() {
    const t = current;
    if (t.outdated) {
      return h("div", {},
        h("div", { class: "rv-snippet-note" }, icon("alert", { size: 14 }), " The text this comment was on is gone. It said this:"),
        quoteTable(t.quote, t.base.line_start));
    }
    const host = h("div", { class: "rv-snippet-host" });
    const fallback = () => h("div", { class: "rv-snippet" }, h("table", { class: "code-table" }, h("tbody", {},
      (t.text || "").split("\n").map((l, i) => h("tr", { class: "is-span" },
        h("td", { class: "ln", dataset: { ln: t.line_start + i } }), h("td", { class: "lc", text: l }))))));
    if (!env.texts) {
      host.append(fallback());
    } else {
      host.append(h("div", { class: "rv-snippet rv-snippet-loading" }, h("span", { class: "spinner" })));
      env.texts.get(t.path).then((text) => {
        let table = null;
        if (text !== null && text.slice(t.start16, t.end16) === t.text) {
          table = snippetTable(text, t.start16, t.end16, { firstLine: t.line_start });
        }
        clear(host).append(table || fallback());
      });
    }
    if (t.changed) {
      const original = h("details", { class: "rv-original" },
        h("summary", {}, "What it said when the comment was made"),
        wordDiffTable(t.quote, t.text, { oldLine: t.base.line_start, newLine: t.line_start }));
      return h("div", {}, host, original);
    }
    return host;
  }

  function commentItem(c) {
    if (c.kind !== "comment") return eventItem(c);
    const mine = isMe(c.author) && env.perms.comment;
    const bodyHost = h("div", { class: "rv-comment-body" });
    const paintBody = () => {
      clear(bodyHost);
      if (c.body) bodyHost.append(h("div", { class: "markdown", html: renderMarkdown(c.body, { mentions: c.mentions }) }));
      if (c.suggestion !== null && c.suggestion !== undefined) bodyHost.append(suggestionBlock(c));
    };
    paintBody();
    let menu = null;
    if (mine) {
      menu = dropdown({
        summary: icon("kebab"),
        summaryClass: "btn-octicon",
        label: "Comment actions",
        align: "right",
        items: [
          { text: "Edit", icon: "pencil", onClick: () => editComment(c, bodyHost, paintBody) },
          { text: "Delete", icon: "trash", danger: true, onClick: () => deleteComment(c) },
        ],
      });
    }
    return h("div", { class: "rv-comment", id: `comment-${c.id}` },
      avatar(c.author, 32),
      h("div", { class: "rv-comment-main" },
        h("div", { class: "rv-comment-head" },
          h("strong", { text: personName(c.author) }),
          h("span", { class: "color-muted text-small" }, timeAgo(c.created_at)),
          c.edited_at ? h("span", { class: "color-muted text-small", title: "Edited", text: "· edited" }) : null,
          h("span", { class: "flex-1" }),
          menu),
        bodyHost));
  }

  function eventItem(c) {
    const [iconName, says, cls] = EVENTS[c.kind] || ["dot", c.kind, ""];
    const note = c.body && !(c.kind === "applied" && c.body === APPLIED_NOTE) ? c.body : "";
    return h("div", { class: `rv-event ${cls}` },
      h("span", { class: "rv-event-badge" }, icon(iconName, { size: 14 })),
      h("div", { class: "rv-event-main" },
        h("div", {}, h("strong", { text: personName(c.author) }), ` ${says} `, h("span", { class: "color-muted" }, timeAgo(c.created_at))),
        note ? h("div", { class: "rv-event-note markdown", html: renderMarkdown(note, { mentions: c.mentions }) }) : null));
  }

  function suggestionBlock(c) {
    const t = current;
    const old = t.outdated ? t.quote : t.text;
    const can = threadActions(t, env.perms);
    const apply = can.apply
      ? h("button", { type: "button", class: "btn btn-sm", title: "Write this text into the live document and resolve the conversation" }, icon("check", { size: 14 }), "Apply suggestion")
      : null;
    apply?.addEventListener("click", () => busy(apply, () => act(
      api.post(url(`/threads/${enc(t.id)}/apply`), { comment_id: c.id }), `Applied the suggestion to ${t.path}.`)));
    const same = old === c.suggestion;
    return h("div", { class: "rv-suggestion" },
      h("div", { class: "rv-suggestion-head" }, icon("pencil", { size: 14 }), h("span", { class: "flex-1", text: c.suggestion === "" ? "Suggested deletion" : "Suggested change" }), apply),
      same ? h("div", { class: "rv-suggestion-same", text: "The text says this already." })
        : wordDiffTable(old, c.suggestion, { oldLine: t.outdated ? t.base.line_start : t.line_start, newLine: t.outdated ? t.base.line_start : t.line_start }));
  }

  function editComment(c, bodyHost, paintBody) {
    const form = commentForm({
      body: c.body,
      suggestion: c.suggestion ?? null,
      prefill: current.outdated ? current.quote : current.text,
      allowSuggestion: !current.outdated || c.suggestion !== null,
      label: "Edit the comment",
      projectId: env.project.id,
      buttons: [{
        text: "Save", primary: true,
        onClick: async (v) => {
          const next = await api.patch(url(`/comments/${enc(c.id)}`), { body: v.body, suggestion: v.suggestion });
          update(next);
        },
      }],
      onCancel: paintBody,
    });
    clear(bodyHost).append(form.el);
    form.focus();
  }

  async function deleteComment(c) {
    const only = current.comments.filter((x) => x.kind === "comment").length === 1;
    const ok = await confirmDialog({
      title: "Delete this comment?",
      message: only ? "It is the only comment, so the whole conversation goes with it." : "The comment goes for everybody.",
      confirmText: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.del(url(`/comments/${enc(c.id)}`));
      update(res?.thread || null);
    } catch (err) {
      flashError(err);
    }
  }

  function resolution() {
    const t = current;
    const r = t.resolution;
    if (!r) return null;
    const table = r.diff ? storedDiffTable(r.diff, t.quote) : null;
    return h("div", { class: "rv-resolution" },
      h("div", { class: "rv-resolution-head" },
        icon("arrow-switch", { size: 14, cls: "color-muted" }),
        h("strong", { text: "How it was changed" }),
        h("span", { class: "color-muted text-small" },
          `${personName(r.by)} resolved it `, timeAgo(r.at),
          r.commit ? [" in ", h("a", { class: "sha", href: `${env.base}/commit/${enc(r.commit)}`, text: r.commit.slice(0, 7) })] : null)),
      table || h("div", { class: "rv-resolution-empty", text: "The text did not change." }));
  }

  function footer() {
    const t = current;
    const can = threadActions(t, env.perms);
    if (!can.reply && !can.resolve && !can.accept && !can.reopen) return null;
    const replyHost = h("div", { class: "rv-reply" });
    const paintReply = () => {
      clear(replyHost);
      if (!can.reply) return;
      if (!replyOpen) {
        const fake = h("button", { type: "button", class: "form-control input-block rv-reply-fake", text: "Reply…" });
        fake.addEventListener("click", () => {
          replyOpen = true;
          paintReply();
        });
        replyHost.append(avatar(session.me, 32), fake);
        return;
      }
      const form = commentForm({
        prefill: t.outdated ? t.quote : t.text,
        allowSuggestion: !t.outdated,
        placeholder: "Reply",
        label: "Reply",
        projectId: env.project.id,
        buttons: [{
          text: "Reply", primary: true,
          onClick: async (v) => {
            const body = { body: v.body };
            if (v.suggestion !== null) body.suggestion = v.suggestion;
            const next = await api.post(url(`/threads/${enc(t.id)}/comments`), body);
            replyOpen = false;
            update(next);
          },
        }],
        onCancel: () => {
          replyOpen = false;
          paintReply();
        },
      });
      replyHost.append(avatar(session.me, 32), form.el);
      form.focus();
    };
    paintReply();

    const actions = h("div", { class: "rv-thread-actions" });
    if (can.resolve) {
      const b = h("button", { type: "button", class: "btn btn-sm" }, icon("check", { size: 14 }), "Resolve conversation");
      b.addEventListener("click", async () => {
        const note = await noteDialog({
          title: "Resolve this conversation",
          text: "meertex keeps how the text changed since the comment, so the reviewer sees it next to the request and can accept it or reopen the conversation.",
          confirmText: "Resolve conversation",
          placeholder: "What did you change?",
          projectId: env.project.id,
        });
        if (note === null) return;
        await busy(b, () => act(api.post(url(`/threads/${enc(t.id)}/resolve`), { note })));
      });
      actions.append(b);
    }
    if (can.accept) {
      const b = h("button", { type: "button", class: "btn btn-sm", title: "You are content with how this was handled" }, icon("check-circle", { size: 14 }), "Accept");
      b.addEventListener("click", () => busy(b, () => act(api.post(url(`/threads/${enc(t.id)}/accept`), {}))));
      actions.append(b);
    }
    if (can.reopen) {
      const b = h("button", { type: "button", class: "btn btn-sm" }, icon("history", { size: 14 }), "Reopen");
      b.addEventListener("click", async () => {
        const note = await noteDialog({
          title: "Reopen this conversation",
          text: "Say what is still missing, so the authors know what to do.",
          confirmText: "Reopen",
          placeholder: "What is still missing?",
          projectId: env.project.id,
        });
        if (note === null) return;
        await busy(b, () => act(api.post(url(`/threads/${enc(t.id)}/reopen`), { note })));
      });
      actions.append(b);
    }
    let hint = null;
    if (t.state === "resolved" && can.accept) hint = "Check the change below, then accept it or reopen the conversation.";
    else if (t.pending) hint = "Pending until you finish your review.";
    return h("div", { class: "rv-thread-foot" },
      replyHost,
      actions.childNodes.length || hint ? h("div", { class: "rv-thread-bar" },
        hint ? h("span", { class: "text-small color-muted flex-1", text: hint }) : h("span", { class: "flex-1" }),
        actions) : null);
  }

  function paint() {
    const t = current;
    card.classList.toggle("is-collapsed", collapsed);
    card.classList.toggle("is-accepted", t.state === "accepted");
    card.classList.toggle("is-pending", Boolean(t.pending));
    card.setAttribute("aria-label", `Comment on ${t.outdated ? t.base.path : t.path}, ${linesText(t.line_start, t.line_end)}`);
    const body = collapsed ? null : h("div", { class: "rv-thread-body" },
      opts.snippet === false ? null : snippet(),
      h("div", { class: "rv-timeline" }, t.comments.map(commentItem)),
      resolution(),
      footer());
    card.replaceChildren(header(), body || "");
  }

  card.addEventListener("click", (e) => {
    if (!env.onSelect) return;
    if (e.target.closest("a, button, input, textarea, select, summary, details, .dropdown-menu")) return;
    env.onSelect(current, card);
  });

  card.threadData = () => current;
  card.update = update;
  card.expand = () => {
    if (collapsed) {
      collapsed = false;
      paint();
    }
  };
  paint();
  return card;
}

// --- Highlighting a thread named in the URL ---------------------------------------

// `#thread-<id>`: open the card, mark it and keep it marked until another one is named.
export function highlightHash(root) {
  root.querySelectorAll(".rv-thread.is-target").forEach((el) => el.classList.remove("is-target"));
  const id = decodeURIComponent(location.hash.slice(1));
  if (!id.startsWith("thread-")) return null;
  const card = root.querySelector(`#${CSS.escape(id)}`);
  if (!card) return null;
  card.expand?.();
  card.classList.add("is-target");
  return card;
}

// --- Open thread counts -------------------------------------------------------------

const openCounts = new Map();
const COUNT_TTL = 20000;

export function openThreadCount(projectId) {
  const hit = openCounts.get(projectId);
  if (hit && Date.now() - hit.at < COUNT_TTL) return hit.promise;
  // The project's summary counts them in the database. Listing the threads
  // would anchor every one of them (a diff each) only to count them.
  const promise = api.get(projectUrl(projectId))
    .then((project) => project.open_threads ?? 0)
    .catch(() => null);
  openCounts.set(projectId, { at: Date.now(), promise });
  return promise;
}

function fillCount(el) {
  openThreadCount(el.dataset.openThreads).then((n) => {
    if (n === null) return;
    el.textContent = String(n);
    el.hidden = false;
  });
}

// The counter of the Reviews tab: open conversations the reader can see.
export function openThreadCounter(projectId) {
  const el = counter("", "rv-open-count");
  el.hidden = true;
  el.dataset.openThreads = projectId;
  el.title = "Open conversations";
  fillCount(el);
  return el;
}

// After a change, every counter on the page asks again.
export function refreshOpenCounts(projectId) {
  openCounts.delete(projectId);
  document.querySelectorAll(`[data-open-threads="${CSS.escape(projectId)}"]`).forEach(fillCount);
}

// "N open conversations" for the Overview's sidebar.
export function openConversationsSection(project, base) {
  const line = h("div", { class: "text-small color-muted", text: "…" });
  openThreadCount(project.id).then((n) => {
    if (n === null) {
      line.textContent = "";
      return;
    }
    clear(line).append(n
      ? h("a", { href: `${base}/reviews`, class: "text-bold" }, icon("comment", { size: 14 }), ` ${plural(n, "open conversation")}`)
      : h("span", { text: "No open conversations." }));
  });
  return h("div", { class: "sidebar-section" },
    h("h2", {}, h("a", { href: `${base}/reviews` }, "Reviews")),
    line);
}
