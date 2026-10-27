// The References tab (docs/DESIGN.md §16, In the web app), like GitHub's issue
// list. /p/<name>/references shows the last check at the top, a filter by
// status and a search, and one row per .bib entry. A row opens to its
// findings, the record next to the entry, the claims and the checked mark.
//
// The .bib files stay the source. Every write here ("Take this value", the
// checked mark, a new entry) is an edit of them made by the server, and the
// report is read again after it, because one change can change other entries
// too (a duplicate, a key).

import { api, enc, projectUrl } from "../api.js";
import { h, append, clear, debounce, nextId, plural } from "../dom.js";
import { icon } from "../icons.js";
import { onLeave, setQuery } from "../router.js";
import { timeAgo } from "../time.js";
import {
  blankslate, busy, confirmDialog, counter, field, flash, flashBox, flashError, openDialog, personName, select,
  setTitle, spinner, textInput,
} from "../ui.js";
import { editorHref } from "./editor.js";

const POLL_MS = 1500;
const COUNT_TTL = 20000;

const STATUS = {
  error: { text: "Error", many: "errors", cls: "label-danger" },
  changed: { text: "Changed", many: "changed", cls: "label-attention" },
  warning: { text: "Warning", many: "warnings", cls: "label-attention" },
  pending: { text: "Pending", many: "pending", cls: "label-secondary" },
  unverified: { text: "Unverified", many: "unverified", cls: "label-secondary" },
  verified: { text: "Verified", many: "verified", cls: "label-success" },
  checked: { text: "Checked", many: "checked", cls: "label-success" },
};
const STATUS_ORDER = ["error", "changed", "warning", "pending", "unverified", "verified", "checked"];

const FILTERS = [
  ["all", "All", null],
  // Broader than the tab's counter, which leaves warnings out.
  ["problems", "Needs a look", ["error", "changed", "warning"]],
  ["pending", "Pending", ["pending"]],
  ["verified", "Verified", ["verified"]],
  ["checked", "Checked", ["checked"]],
  ["unverified", "Unverified", ["unverified"]],
];

const VERDICTS = {
  supported: { text: "Supported", cls: "label-success" },
  partly: { text: "Partly supported", cls: "label-attention" },
  not_supported: { text: "Not supported", cls: "label-danger" },
  unclear: { text: "Unclear", cls: "label-secondary" },
  no_source: { text: "No source", cls: "label-secondary" },
};

const LEVEL_ICONS = { error: "x-circle", warning: "alert", info: "info" };

// The entry's fields next to the record's, in this order. `fields` are the
// entry's names for it, the first one it has wins.
const COMPARE = [
  { label: "Title", fields: ["title"], record: (r) => r.title },
  { label: "Authors", fields: ["author"], record: (r) => (r.authors || []).join(", "), people: true },
  { label: "Year", fields: ["year", "date"], record: (r) => r.year },
  { label: "Venue", fields: ["journal", "journaltitle", "booktitle"], record: (r) => r.venue },
  { label: "Volume", fields: ["volume"], record: (r) => r.volume },
  { label: "Number", fields: ["number"], record: (r) => r.number },
  { label: "Pages", fields: ["pages"], record: (r) => r.pages },
  { label: "Publisher", fields: ["publisher"], record: (r) => r.publisher },
  { label: "DOI", fields: ["doi"], record: (r) => r.doi },
];

// --- LaTeX as plain text, for showing titles and names --------------------------------

const ACCENTS = {
  '"': "̈", "'": "́", "`": "̀", "^": "̂", "~": "̃", "=": "̄", ".": "̇",
  u: "̆", v: "̌", H: "̋", c: "̧", k: "̨", r: "̊",
};
const LETTERS = { ss: "ß", ae: "æ", AE: "Æ", oe: "œ", OE: "Œ", aa: "å", AA: "Å", o: "ø", O: "Ø", l: "ł", L: "Ł", i: "i", j: "j" };

// "{BERT}: Pre-training of {D}eep" -> "BERT: Pre-training of Deep". Accents,
// escaped characters and dashes become what they print, other commands go.
export function plainText(value) {
  let s = String(value ?? "");
  s = s.replace(/\\\\/g, " ");
  s = s.replace(/\\(TeX|LaTeX|BibTeX|XeTeX|LuaTeX)(?![A-Za-z])/g, "$1");
  s = s.replace(/\\(ss|ae|AE|oe|OE|aa|AA|o|O|l|L|i|j)(?![A-Za-z])\s*/g, (_, l) => LETTERS[l]);
  s = s.replace(/\\(["'`^~=.])\s*\{?\s*([A-Za-z])\s*\}?/g, (_, a, ch) => ch + ACCENTS[a]);
  s = s.replace(/\\([uvHckr])\s*\{\s*([A-Za-z])\s*\}/g, (_, a, ch) => ch + ACCENTS[a]);
  s = s.replace(/\\\{/g, "\u0001").replace(/\\\}/g, "\u0002");
  s = s.replace(/\\([&%$#_])/g, "$1");
  s = s.replace(/\\[A-Za-z]+\*?\s*/g, "");
  s = s.replace(/[{}$]/g, "").replace(/\u0001/g, "{").replace(/\u0002/g, "}");
  s = s.replace(/~/g, " ").replace(/---/g, "\u2014").replace(/--/g, "\u2013");
  return s.normalize("NFC").replace(/\s+/g, " ").trim();
}

function people(value) {
  const text = plainText(value);
  return text ? text.split(/\s+and\s+/i).map((n) => n.trim()).filter(Boolean) : [];
}

function isOthers(name) {
  return /^others$/i.test(name);
}

function familyName(name) {
  if (name.includes(",")) return name.split(",")[0].trim();
  const parts = name.split(/\s+/);
  return parts[parts.length - 1];
}

// "Vaswani, Ashish" -> "Ashish Vaswani".
function fullName(name) {
  if (isOthers(name)) return "others";
  const parts = name.split(",").map((p) => p.trim());
  if (parts.length === 2) return `${parts[1]} ${parts[0]}`.trim();
  if (parts.length === 3) return `${parts[2]} ${parts[0]}, ${parts[1]}`.trim();
  return name;
}

// "Vaswani, Shazeer and Parmar", or "Vaswani, Shazeer, Parmar et al." for more.
function firstAuthors(value) {
  const list = people(value);
  const others = list.some(isOthers);
  const names = list.filter((n) => !isOthers(n)).map(familyName);
  if (!names.length) return "";
  if (names.length > 3) return `${names.slice(0, 3).join(", ")} et al.`;
  if (others) return `${names.join(", ")} et al.`;
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function yearOf(entry) {
  const f = entry.fields || {};
  const m = /\d{4}/.exec(plainText(f.year || f.date || ""));
  return m ? m[0] : "";
}

function fold(text) {
  return String(text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

// Links from records and pages, never javascript: or data:.
function safeUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url) ? url : null;
}

function lineHref(base, path, line) {
  return `${editorHref(base, path)}${line ? `#L${line}` : ""}`;
}

function where(path, line) {
  return `${path}${line ? `:${line}` : ""}`;
}

// --- The tab's counter ----------------------------------------------------------------

const problemCounts = new Map();

function problemCount(projectId) {
  const hit = problemCounts.get(projectId);
  if (hit && Date.now() - hit.at < COUNT_TTL) return hit.promise;
  const promise = api.get(projectUrl(projectId, "/references/summary"))
    .then((s) => (typeof s?.problems === "number" ? s.problems : null))
    .catch(() => null);
  problemCounts.set(projectId, { at: Date.now(), promise });
  return promise;
}

function paintCount(el, n) {
  if (n === null || n === undefined) return;
  el.textContent = String(n);
  el.hidden = !n;
  el.classList.toggle("counter-attention", n > 0);
}

// The counter of the References tab: the entries that are error or changed,
// and the broken ones. Hidden at 0.
export function problemsCounter(projectId) {
  const el = counter("", "rf-problem-count");
  el.hidden = true;
  el.dataset.refProblems = projectId;
  el.title = "References with problems";
  problemCount(projectId).then((n) => paintCount(el, n));
  return el;
}

// The tab knows the number from its report, so the counters need not ask.
function setProblemCount(projectId, n) {
  problemCounts.set(projectId, { at: Date.now(), promise: Promise.resolve(n) });
  document.querySelectorAll(`[data-ref-problems="${CSS.escape(projectId)}"]`).forEach((el) => paintCount(el, n));
}

// --- The page -------------------------------------------------------------------------

export async function render(ctx) {
  const p = ctx.project;
  setTitle("References", p.name);
  const report = await api.get(projectUrl(ctx.ref, "/references"));
  if (!ctx.alive()) return;

  const status = FILTERS.some(([k]) => k === ctx.query.get("status")) ? ctx.query.get("status") : "all";
  const state = {
    ctx,
    report,
    status,
    q: ctx.query.get("q") || "",
    open: new Set(),
    timer: 0,
    seq: 0,
    dead: false,
    el: {},
  };
  // A link to #ref-<key> opens that entry.
  const linked = location.hash.startsWith("#ref-") ? decodeURIComponent(location.hash.slice(5)) : "";
  for (const e of report.entries) if (e.key === linked) state.open.add(rowId(e));

  build(state);
  paint(state);
  clear(ctx.main).append(state.el.root);
  setProblemCount(p.id, report.problems);
  watch(state);
  onLeave(() => {
    state.dead = true;
    clearTimeout(state.timer);
  });
}

function rowId(entry) {
  return `${entry.path}\n${entry.key}`;
}

// The parts that stay while the page lives: the search keeps its focus and
// caret when a check ends and the report is painted again.
function build(state) {
  const el = state.el;
  el.head = h("div");
  el.notes = h("div");
  el.search = h("input", {
    type: "search", class: "form-control", placeholder: "Find a reference…", value: state.q,
    "aria-label": "Find a reference by key, title or author",
  });
  const run = debounce(() => {
    state.q = el.search.value.trim();
    setQuery({ q: state.q });
    paintList(state);
  }, 150);
  el.search.addEventListener("input", run);
  el.filter = h("div", { class: "segmented rf-filter", role: "group", "aria-label": "Which entries" });
  el.filterBar = h("div", { class: "filter-bar rf-filter-bar" }, h("div", { class: "search" }, icon("search"), el.search), el.filter);
  el.list = h("div", { class: "rf-list" });
  el.sections = h("div");
  el.root = h("div", { class: "rf-page" }, el.head, el.notes, el.filterBar, el.list, el.sections);
}

function paint(state) {
  const { report } = state;
  const el = state.el;
  append(clear(el.head), [head(state)]);
  append(clear(el.notes), [notes(state)]);
  const empty = !report.entries.length;
  el.filterBar.hidden = empty;
  paintFilter(state);
  paintList(state);
  append(clear(el.sections), [brokenSection(state), missingSection(state)]);
}

// --- Header: title, counts, buttons and the last run ----------------------------------

function canWrite(state) {
  return state.ctx.canEdit;
}

function head(state) {
  const { report } = state;
  const actions = [];
  if (canWrite(state) && report.network_enabled) {
    const running = report.run?.status === "running";
    const add = h("button", { type: "button", class: "btn" }, icon("plus"), "Add a reference");
    add.addEventListener("click", () => addDialog(state));
    actions.push(add);
    if (report.claims_enabled) {
      const claims = h("button", { type: "button", class: "btn rf-check", disabled: running }, icon("comment"), "Check claims");
      claims.addEventListener("click", () => startCheck(state, claims, true));
      actions.push(claims);
    }
    const check = h("button", { type: "button", class: "btn btn-primary rf-check", disabled: running }, icon("check-circle"), "Check references");
    check.addEventListener("click", () => startCheck(state, check, false));
    actions.push(check);
  }
  const title = h("div", { class: "subhead rf-subhead" },
    h("h2", {}, "References ", counter(report.entries.length)),
    actions.length ? h("div", { class: "rf-actions" }, actions) : null);

  const counts = STATUS_ORDER.filter((s) => report.counts?.[s]).map((s) => {
    const n = report.counts[s];
    return h("span", { class: `Label ${STATUS[s].cls}`, dataset: { status: s } }, `${n} ${n === 1 ? STATUS[s].text.toLowerCase() : STATUS[s].many}`);
  });
  if (report.broken.length) {
    counts.push(h("span", { class: "Label label-danger", dataset: { status: "broken" } }, plural(report.broken.length, "broken entry", "broken entries")));
  }
  state.el.run = h("div", { class: "rf-run text-small color-muted", role: "status" });
  paintRun(state);
  return [title, h("div", { class: "rf-summary" }, counts.length ? h("div", { class: "rf-counts" }, counts) : null, state.el.run)];
}

function paintRun(state) {
  const run = state.report.run;
  const box = state.el.run;
  if (!box) return;
  clear(box);
  state.el.root?.querySelectorAll(".rf-check").forEach((b) => { b.disabled = run?.status === "running"; });
  if (!run) {
    box.append(icon("clock", { size: 14 }), h("span", { text: "Not checked yet." }));
    return;
  }
  const who = run.created_by ? personName(run.created_by) : null;
  if (run.status === "running") {
    const total = run.total || 0;
    const done = Math.min(run.done || 0, total);
    const pct = total ? Math.round((done / total) * 100) : 0;
    box.append(
      spinner(),
      h("span", { text: run.claims ? "Checking the references and their claims" : "Checking the references" }),
      h("span", {
        class: "rf-progress", role: "progressbar", "aria-label": "Progress of the check",
        "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(done),
      }, h("span", { style: { width: `${pct}%` } })),
      h("span", { class: "rf-progress-text", text: total ? `${done} of ${total}` : "Starting" }));
    return;
  }
  if (run.status === "failed") {
    box.append(icon("alert", { size: 14, cls: "color-danger" }),
      h("span", {}, "The last check failed ", timeAgo(run.finished_at || run.started_at), ". ", run.error || ""));
    return;
  }
  box.append(icon("check-circle", { size: 14 }),
    h("span", {}, "Last checked ", timeAgo(run.finished_at || run.started_at),
      who ? [" by ", h("strong", { class: "color-fg", text: who })] : "",
      run.claims ? ", with the claims" : "", "."));
}

function notes(state) {
  const { report } = state;
  const out = [];
  if (canWrite(state) && !report.network_enabled) {
    out.push(flashBox("Reference lookups are off on this server, so meertex cannot check entries against the publishers' records or add new ones here. The checks of the .bib files themselves still run.", "info", { iconName: "info" }));
  }
  if (report.entries.length && !report.run) {
    out.push(flashBox(canWrite(state) && report.network_enabled
      ? "Nothing is checked yet. Check references looks every entry up at doi.org, Crossref, Semantic Scholar, DBLP, arXiv and Open Library and compares it with the record."
      : "Nothing is checked yet. The findings below come from the .bib files alone.", "info"));
  }
  return out.length ? h("div", { class: "rf-notes" }, out) : null;
}

// --- Runs -----------------------------------------------------------------------------

async function startCheck(state, button, claims) {
  if (claims) {
    const ok = await confirmDialog({
      title: "Check the claims",
      message: h("div", {},
        h("p", { text: "Claude reads every sentence that cites an entry, together with the entry's abstract, and says whether the source supports it." }),
        h("p", { text: "Every check of claims asks Claude, and that costs money. Sentences that did not change since the last check are not asked again." })),
      confirmText: "Check claims",
    });
    if (!ok || state.dead) return;
  }
  try {
    const run = await busy(button, () => api.post(projectUrl(state.ctx.ref, "/references/check"), claims ? { claims: true } : {}));
    if (state.dead) return;
    state.report.run = run;
    paintRun(state);
    watch(state);
  } catch (err) {
    flashError(err);
  }
}

// While a run is running, ask for it every 1.5 seconds. When it ends, read
// the report again and paint it in place.
function watch(state) {
  clearTimeout(state.timer);
  const run = state.report.run;
  if (state.dead || run?.status !== "running") return;
  state.timer = setTimeout(async () => {
    let next;
    try {
      next = await api.get(projectUrl(state.ctx.ref, `/references/check/${enc(run.id)}`));
    } catch (err) {
      if (state.dead || err.status === 404 || err.status === 401) return;
      watch(state);
      return;
    }
    if (state.dead) return;
    if (next.status === "running") {
      state.report.run = next;
      paintRun(state);
      watch(state);
      return;
    }
    await reload(state);
  }, POLL_MS);
}

async function reload(state, { focus = null } = {}) {
  const seq = ++state.seq;
  let report;
  try {
    report = await api.get(projectUrl(state.ctx.ref, "/references"));
  } catch (err) {
    if (!state.dead) flashError(err);
    return;
  }
  if (state.dead || seq !== state.seq) return;
  state.report = report;
  paint(state);
  setProblemCount(state.ctx.project.id, report.problems);
  watch(state);
  if (focus) {
    const row = state.el.list.querySelector(`[data-row="${CSS.escape(focus)}"]`);
    row?.querySelector(".rf-toggle")?.focus();
  }
}

// --- The filter and the list ----------------------------------------------------------

function paintFilter(state) {
  const counts = state.report.counts || {};
  const count = (statuses) => (statuses ? statuses.reduce((n, s) => n + (counts[s] || 0), 0) : state.report.entries.length);
  append(clear(state.el.filter), FILTERS.map(([key, text, statuses]) => h("button", {
    type: "button", "aria-pressed": String(key === state.status),
    onclick: () => {
      state.status = key;
      setQuery({ status: key === "all" ? null : key });
      paintFilter(state);
      paintList(state);
    },
  }, text, h("span", { class: "rf-filter-count", text: ` ${count(statuses)}` }))));
}

function matches(state, entry) {
  const statuses = FILTERS.find(([k]) => k === state.status)?.[2];
  if (statuses && !statuses.includes(entry.status)) return false;
  if (!state.q) return true;
  const f = entry.fields || {};
  const hay = fold(`${entry.key} ${plainText(f.title)} ${plainText(f.author || f.editor)}`);
  return fold(state.q).split(/\s+/).every((w) => hay.includes(w));
}

function paintList(state) {
  const { report } = state;
  const list = state.el.list;
  clear(list);
  if (!report.entries.length) {
    list.append(h("div", { class: "box" }, report.files.length
      ? blankslate({
        iconName: "quote",
        title: "No entries yet",
        text: `${report.files.join(", ")} ${report.files.length === 1 ? "has" : "have"} no entries. Add one here or write it in the editor.`,
        action: addButton(state, "Add a reference"),
      })
      : blankslate({
        iconName: "quote",
        title: "No .bib files",
        text: "This paper has no .bib file yet. Add a reference here and meertex writes it into refs.bib, or make a .bib file in the editor.",
        action: addButton(state, "Add the first reference"),
      })));
    return;
  }
  const shown = report.entries.filter((e) => matches(state, e));
  if (!shown.length) {
    const clearAll = h("button", { type: "button", class: "btn", text: "Clear the filter" });
    clearAll.addEventListener("click", () => {
      state.status = "all";
      state.q = "";
      state.el.search.value = "";
      setQuery({ status: null, q: null });
      paintFilter(state);
      paintList(state);
    });
    list.append(h("div", { class: "box" }, blankslate({
      iconName: "search", title: "No entries match", text: "Try another search, or show all entries.", action: clearAll,
    })));
    return;
  }
  list.append(h("ul", { class: "box box-list", "aria-label": "Entries" }, shown.map((e) => entryRow(state, e))));
}

function addButton(state, text) {
  if (!canWrite(state) || !state.report.network_enabled) return null;
  return h("button", { type: "button", class: "btn btn-primary", text, onclick: () => addDialog(state) });
}

function statusLabel(status) {
  const s = STATUS[status] || { text: status, cls: "label-secondary" };
  return h("span", { class: `Label ${s.cls}`, text: s.text });
}

function worstLevel(findings) {
  if (findings.some((f) => f.level === "error")) return "error";
  if (findings.some((f) => f.level === "warning")) return "warning";
  return "info";
}

function entryRow(state, entry) {
  const id = rowId(entry);
  const open = state.open.has(id);
  const f = entry.fields || {};
  const title = plainText(f.title) || "Untitled";
  const authors = firstAuthors(f.author || f.editor);
  const year = yearOf(entry);
  const findings = entry.findings || [];
  const detailId = nextId("rf-detail");
  const level = worstLevel(findings);

  const toggle = h("button", {
    type: "button", class: "rf-toggle", "aria-expanded": String(open), "aria-controls": detailId,
  },
  h("span", { class: "rf-status" }, statusLabel(entry.status)),
  h("span", { class: "rf-main" },
    h("span", { class: "rf-line" },
      h("span", { class: "rf-key", text: entry.key }),
      h("span", { class: "rf-title", text: title })),
    h("span", { class: "rf-sub text-small color-muted" },
      [authors, year, where(entry.path, entry.line)].filter(Boolean).join(" · "))),
  findings.length
    ? h("span", { class: `rf-count is-${level}`, title: plural(findings.length, "finding") },
      icon(LEVEL_ICONS[level], { size: 14 }), h("span", { text: String(findings.length) }))
    : null,
  icon("chevron-right", { size: 16, cls: "rf-chevron" }));

  const detail = h("div", { class: "rf-detail", id: detailId, hidden: !open });
  if (open) append(detail, [entryDetail(state, entry)]);
  const row = h("li", { class: `box-row rf-entry${open ? " is-open" : ""}`, id: `ref-${entry.key}`, dataset: { row: id } }, toggle, detail);
  toggle.addEventListener("click", () => {
    const now = !state.open.has(id);
    if (now) state.open.add(id);
    else state.open.delete(id);
    toggle.setAttribute("aria-expanded", String(now));
    row.classList.toggle("is-open", now);
    clear(detail);
    if (now) append(detail, [entryDetail(state, entry)]);
    detail.hidden = !now;
  });
  return row;
}

// --- One entry, opened ----------------------------------------------------------------

function entryDetail(state, entry) {
  const findings = (entry.findings || []).filter((f) => f.code !== "claim");
  const claims = (entry.findings || []).filter((f) => f.code === "claim");
  const base = state.ctx.base;
  const parts = [];

  parts.push(findings.length
    ? h("ul", { class: "rf-findings", "aria-label": "Findings" }, findings.map((f) => findingItem(state, entry, f)))
    : h("p", {
      class: "rf-none color-muted",
      text: entry.record ? "The record matches the entry." : entry.checked_at ? "No findings." : "No findings so far.",
    }));

  if (entry.record) parts.push(compareTable(entry));
  if (claims.length) parts.push(claimList(state, claims, entry));

  // Where it is, when it was looked up, the mark, the citations.
  const facts = [];
  facts.push(h("li", {}, entry.checked_at
    ? ["Looked up ", timeAgo(entry.checked_at), entry.record?.source ? ` at ${entry.record.source}` : "", "."]
    : "Not looked up yet as it reads now."));
  if (entry.mark) facts.push(h("li", {}, markText(entry.mark)));
  if (entry.cites?.length) {
    const shown = entry.cites.slice(0, 6);
    facts.push(h("li", {}, "Cited in ",
      shown.map((c, i) => [i ? ", " : "", h("a", { class: "text-mono", href: lineHref(base, c.path, c.line), text: where(c.path, c.line) })]),
      entry.cites.length > shown.length ? ` and ${entry.cites.length - shown.length} more` : "", "."));
  }
  parts.push(h("ul", { class: "rf-facts text-small color-muted" }, facts));

  const actions = [h("a", { class: "btn btn-sm", href: lineHref(base, entry.path, entry.line) }, icon("pencil"), "Open in the editor")];
  if (canWrite(state)) {
    if (!entry.mark || !entry.mark.valid) {
      const mark = h("button", { type: "button", class: "btn btn-sm" }, icon("check"), entry.mark ? "Mark as checked again" : "Mark as checked");
      mark.addEventListener("click", () => write(state, entry, mark, "POST", "/checked", undefined,
        `Marked ${entry.key} as checked.`));
      actions.push(mark);
    }
    if (entry.mark) {
      const unmark = h("button", { type: "button", class: "btn btn-sm" }, icon("x"), "Remove the mark");
      unmark.addEventListener("click", () => write(state, entry, unmark, "DELETE", "/checked", undefined,
        `Removed the mark from ${entry.key}.`));
      actions.push(unmark);
    }
  }
  parts.push(h("div", { class: "rf-entry-actions" }, actions));
  return parts;
}

function markText(mark) {
  if (!mark.date) return "The checked mark cannot be read. Mark the entry again or remove the mark.";
  const by = mark.by || "someone";
  if (mark.valid) return `Marked as checked by ${by} on ${mark.date}.`;
  return `${by} marked it as checked on ${mark.date}. The entry changed since.`;
}

function findingItem(state, entry, f) {
  const meta = [];
  if (f.source) meta.push(h("span", { class: "Label label-secondary", text: f.source }));
  if (f.field) meta.push(h("span", { class: "text-mono", text: f.field }));
  const url = safeUrl(f.url);
  if (url) {
    const text = f.source === "wayback" ? "Open the archived copy" : f.source === "web" ? "Open the page" : "Open the record";
    meta.push(h("a", { href: url, target: "_blank", rel: "noopener", text }));
  }
  let take = null;
  if (f.field && f.record_value && f.code !== "duplicate") {
    const value = h("code", { class: "rf-value", text: f.record_value });
    let button = null;
    if (canWrite(state)) {
      button = h("button", { type: "button", class: "btn btn-sm" }, "Take this value");
      button.addEventListener("click", () => write(state, entry, button, "POST", "/fields", { field: f.field, value: f.record_value },
        `Set ${f.field} of ${entry.key}.`));
    }
    take = h("div", { class: "rf-take" }, h("span", { class: "text-small color-muted", text: f.code === "published" || f.code === "suggest_doi" ? "The DOI" : "The record has" }), value, button);
  }
  return h("li", { class: `rf-finding is-${f.level}` },
    icon(LEVEL_ICONS[f.level] || "info", { size: 16, label: f.level }),
    h("div", { class: "rf-finding-body" },
      h("div", { class: "rf-finding-message", text: f.message }),
      meta.length ? h("div", { class: "rf-finding-meta" }, meta) : null,
      take));
}

function compareTable(entry) {
  const f = entry.fields || {};
  const r = entry.record;
  const flagged = new Set((entry.findings || []).filter((x) => x.level !== "info" && x.field).map((x) => x.field));
  const rows = [];
  for (const c of COMPARE) {
    const name = c.fields.find((n) => f[n]);
    const mine = name ? (c.people ? people(f[name]).map(fullName).join(", ") : plainText(f[name])) : "";
    const theirs = String(c.record(r) ?? "");
    if (!mine && !theirs) continue;
    const differs = c.fields.some((n) => flagged.has(n));
    rows.push(h("tr", { class: differs ? "is-differs" : null },
      h("th", { scope: "row", text: c.label }),
      h("td", { text: mine || "–", class: mine ? null : "color-subtle" }),
      h("td", { text: theirs || "–", class: theirs ? null : "color-subtle" })));
  }
  const link = safeUrl(r.url) || (r.doi ? `https://doi.org/${r.doi}` : null);
  return h("div", { class: "rf-compare-box" },
    h("div", { class: "rf-compare-scroll" },
      h("table", { class: "rf-compare" },
        h("thead", {}, h("tr", {},
          h("th", { scope: "col", text: "Field" }),
          h("th", { scope: "col", text: "This entry" }),
          h("th", { scope: "col" }, "The record",
            r.source ? h("span", { class: "color-muted text-normal", text: ` from ${r.source}` }) : null,
            link ? [" ", h("a", { href: link, target: "_blank", rel: "noopener", "aria-label": "Open the record", title: "Open the record" }, icon("link", { size: 12 }))] : null))),
        h("tbody", {}, rows))));
}

function claimList(state, claims, entry) {
  const base = state.ctx.base;
  return h("div", { class: "rf-claims" },
    h("h4", { class: "rf-claims-head" }, "Claims ", counter(claims.length),
      entry.claims_at ? h("span", { class: "text-small color-muted text-normal" }, " checked ", timeAgo(entry.claims_at)) : null),
    h("ul", { class: "rf-claim-list" }, claims.map((c) => {
      const v = VERDICTS[c.verdict] || { text: c.verdict || "Unclear", cls: "label-secondary" };
      return h("li", { class: `rf-claim is-${c.level}` },
        h("div", { class: "rf-claim-head" },
          h("span", { class: `Label ${v.cls}`, text: v.text }),
          c.path ? h("a", { class: "text-mono text-small", href: lineHref(base, c.path, c.line), title: "Open the sentence in the editor", text: where(c.path, c.line) }) : null),
        c.value ? h("blockquote", { class: "rf-quote", text: c.value }) : null,
        c.message ? h("p", { class: "rf-claim-reason", text: c.message }) : null,
        c.record_value ? h("div", { class: "rf-passage" },
          h("div", { class: "text-small color-muted", text: "The passage it relied on" }),
          h("blockquote", { class: "rf-quote rf-quote-source", text: c.record_value })) : null);
    })));
}

// A write into the .bib file, then the report again, with the focus back on the row.
async function write(state, entry, button, method, rest, body, done) {
  const path = projectUrl(state.ctx.ref, `/references/${enc(entry.key)}${rest}`);
  try {
    await busy(button, () => (method === "DELETE" ? api.del(path) : api.post(path, body)));
  } catch (err) {
    flashError(err);
    return;
  }
  if (state.dead) return;
  flash(done, "success");
  await reload(state, { focus: rowId(entry) });
}

// --- Broken entries and missing keys --------------------------------------------------

function brokenSection(state) {
  const broken = state.report.broken || [];
  if (!broken.length) return null;
  const base = state.ctx.base;
  return h("section", { class: "rf-section", "aria-labelledby": "rf-broken" },
    h("div", { class: "rf-section-head" }, h("h3", { id: "rf-broken" }, "Broken entries ", counter(broken.length))),
    h("p", { class: "text-small color-muted", text: "These entries cannot be read, so neither bibtex nor meertex sees them. The rest of the file is read." }),
    h("ul", { class: "box box-list" }, broken.map((b) => h("li", { class: "box-row box-row-sm rf-problem-row" },
      icon("x-circle", { size: 16, cls: "color-danger" }),
      h("div", { class: "flex-1" },
        h("div", { class: "rf-line" },
          b.key ? h("span", { class: "rf-key", text: b.key }) : null,
          h("a", { class: "text-mono text-small", href: lineHref(base, b.path, b.line), text: where(b.path, b.line) })),
        h("div", { class: "text-small", text: b.message }))))));
}

function missingSection(state) {
  const missing = state.report.missing || [];
  if (!missing.length) return null;
  const base = state.ctx.base;
  return h("section", { class: "rf-section", "aria-labelledby": "rf-missing" },
    h("div", { class: "rf-section-head" }, h("h3", { id: "rf-missing" }, "Missing keys ", counter(missing.length))),
    h("p", { class: "text-small color-muted", text: "A \\cite uses these keys, and no entry in the .bib files has them." }),
    h("ul", { class: "box box-list" }, missing.map((m) => h("li", { class: "box-row box-row-sm rf-problem-row" },
      icon("alert", { size: 16, cls: "color-attention" }),
      h("div", { class: "flex-1 rf-line" },
        h("span", { class: "rf-key", text: m.key }),
        h("a", { class: "text-mono text-small", href: lineHref(base, m.path, m.line), title: "Open the \\cite in the editor", text: where(m.path, m.line) }))))));
}

// --- Adding an entry ------------------------------------------------------------------

function defaultBib(report) {
  const per = new Map();
  for (const e of report.entries) per.set(e.path, (per.get(e.path) || 0) + 1);
  let best = null;
  for (const path of [...report.files].sort()) {
    if (best === null || (per.get(path) || 0) > (per.get(best) || 0)) best = path;
  }
  return best;
}

function addDialog(state) {
  const { report } = state;
  const formId = nextId("rf-add");
  const source = textInput({ placeholder: "10.1145/3290605.3300857", autocomplete: "off", spellcheck: "false", maxlength: "2000", required: true });
  const key = textInput({ class: "form-control input-block input-mono", placeholder: "vaswani2017attention", autocomplete: "off", spellcheck: "false", maxlength: "200" });
  const fallback = defaultBib(report);
  const file = select([["", fallback ? `${fallback} (the default)` : "refs.bib (a new file)"], ...report.files.filter((p) => p !== fallback).map((p) => [p, p])], "");
  const message = h("div", { class: "rf-dialog-message", role: "alert" });
  const preview = h("div", { class: "rf-preview", hidden: true });
  const form = h("form", { id: formId, novalidate: true },
    field("DOI, arXiv id, ISBN or link", source, {
      hint: "A link to a page becomes an @online entry, unless the page names its DOI.",
    }),
    h("div", { class: "form-row" },
      field("Key", key, { optional: true, hint: "Made from the first author, the year and the title when empty." }),
      field("File", file, { optional: true })),
    message, preview);
  const submit = h("button", { type: "submit", class: "btn btn-primary", form: formId, text: "Preview" });
  const d = openDialog({
    title: "Add a reference",
    wide: true,
    body: form,
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), submit],
  });

  let previewed = null; // the inputs the preview was made from
  const inputs = () => ({ source: source.value.trim(), key: key.value.trim() || null, path: file.value || null });
  const same = (a, b) => a && b && a.source === b.source && a.key === b.key && a.path === b.path;
  const reset = () => {
    previewed = null;
    preview.hidden = true;
    clear(preview);
    submit.textContent = "Preview";
  };
  [source, key, file].forEach((el) => el.addEventListener("input", reset));
  file.addEventListener("change", reset);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clear(message);
    const body = inputs();
    if (!body.source) {
      message.append(flashBox("Give a DOI, an arXiv id, an ISBN or a link.", "error"));
      source.focus();
      return;
    }
    const adding = same(previewed, body);
    try {
      const result = await busy(submit, () => api.post(projectUrl(state.ctx.ref, "/references"), adding ? body : { ...body, dry_run: true }));
      if (adding) {
        d.close();
        flash(`Added ${result.key} to ${result.path}.`, "success");
        await reload(state);
        openRow(state, result.key, result.path);
        return;
      }
      previewed = body;
      clear(preview).append(
        h("p", { class: "text-small color-muted" }, "It goes into ", h("strong", { class: "color-fg", text: result.path }),
          ` as ${result.key}, at line ${result.line}.`),
        h("pre", { class: "rf-bibtex", text: result.bibtex }));
      preview.hidden = false;
      submit.textContent = "Add";
      submit.focus();
    } catch (err) {
      if (err?.status === 401) return;
      message.append(flashBox(err.message || String(err), err.status === 404 ? "warn" : "error"));
    }
  });
  source.focus();
}

function openRow(state, key, path) {
  const entry = state.report.entries.find((e) => e.key === key && e.path === path);
  if (!entry) return;
  const id = rowId(entry);
  let row = state.el.list.querySelector(`[data-row="${CSS.escape(id)}"]`);
  if (!row) {
    // The filter hides it. Show everything.
    state.status = "all";
    state.q = "";
    state.el.search.value = "";
    setQuery({ status: null, q: null });
    paintFilter(state);
    paintList(state);
    row = state.el.list.querySelector(`[data-row="${CSS.escape(id)}"]`);
  }
  if (!row) return;
  if (!state.open.has(id)) row.querySelector(".rf-toggle").click();
  row.scrollIntoView({ block: "center" });
  row.querySelector(".rf-toggle").focus({ preventScroll: true });
}
