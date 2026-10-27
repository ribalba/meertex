// /timeline is a Gantt chart of all your papers, to see what to do when
// (DESIGN §3, The timeline). One row per paper or plan with its writing
// window, the review, rebuttal, camera-ready and conference as lighter bars,
// and a marker for every date. A writing load strip on top counts how many
// writing windows overlap. Plain DOM, no chart library.
//
// The axis runs in the reader's time zone, one fixed width per day. Bars and
// markers sit at their real instants. Every date a person reads is the
// venue's day (its `local` field or partsIn with its zone), because AoE is
// not a zone the browser knows and "15 Nov AoE" is noon on 16 Nov in Europe.
//
// The same chart, read-only, is the shared timeline at /timeline/<token>:
// public, without a session, and without the rest of the app (DESIGN §3,
// Sharing the timeline). The Share button on /timeline makes its link.
//
// The module also holds the pieces other pages use for plans and writing
// time: the "I need N days" field (settings), and the plan box, the "Start
// the paper" dialog and the writing line of a paper's overview.

import { api, enc, projectUrl } from "../api.js";
import { h, clear, debounce, nextId, plural } from "../dom.js";
import { icon } from "../icons.js";
import { navigate, onLeave } from "../router.js";
import { myTimezone, session } from "../session.js";
import { dateLabel } from "../dates.js";
import { MONTHS, countdown, dayText, formatDue, formatInMyZone, formatShort, partsIn, relative, secondsUntil, urgency } from "../time.js";
import {
  blankslate, busy, commandLine, confirmDialog, copyButton, dropdown, field, flash, flashBox, flashError, loading,
  openDialog, outcomeLabel, queueFlash, select, setTitle, textInput,
} from "../ui.js";
import { plannedLabel, viewTabs } from "./home.js";
import { importSummary, starterChoice } from "./new.js";
import { editorHref } from "./editor.js";

const DAY = 86400000;
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// Pixels per day, and how far the axis reaches before and after today at
// least, in days.
const ZOOMS = {
  weeks: { text: "Weeks", ppd: 28, before: 14, after: 70 },
  months: { text: "Months", ppd: 6, before: 28, after: 240 },
  quarters: { text: "Quarters", ppd: 2, before: 42, after: 540 },
};
// The chart is at least this wide, so a short range still fills a screen.
const MIN_WIDTH = 1200;

// Outcomes after which nothing is left to do (DESIGN §5, The next date).
const CLOSED = new Set(["rejected", "withdrawn", "not_submitted"]);
// Dates something is due at, drawn as diamonds. The others are events (circles).
const DEADLINES = new Set(["abstract", "submission", "supplementary", "rebuttal_end", "revision", "camera_ready", "other"]);

const ANSWERED = {
  yes: ["Done", "label-success"],
  no: ["Did not happen", "label-secondary"],
  accepted: ["Accepted", "label-success"],
  rejected: ["Rejected", "label-danger"],
  revision: ["Revision requested", "label-attention"],
};

const BAR_TEXT = { review: "Review", rebuttal: "Rebuttal", camera: "Camera-ready", conference: "Conference" };

// --- Settings remembered per browser ---------------------------------------

const PREFS_KEY = "meertex.timeline";
const DEFAULT_PREFS = { zoom: "months", folder: "", finished: false };

function loadPrefs() {
  try {
    const stored = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    const prefs = { ...DEFAULT_PREFS, ...(stored && typeof stored === "object" ? stored : {}) };
    if (!ZOOMS[prefs.zoom]) prefs.zoom = DEFAULT_PREFS.zoom;
    if (typeof prefs.folder !== "string") prefs.folder = "";
    prefs.finished = Boolean(prefs.finished);
    return prefs;
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

function savePrefs(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // A private window may refuse storage. The page then forgets, nothing else.
  }
}

// --- Small helpers -----------------------------------------------------------

const px = (n) => `${Math.round(n * 100) / 100}px`;
const due = (d) => Date.parse(d.due_at);

function canEdit(p) {
  return p.role === "owner" || p.role === "editor";
}

function sortedDates(p) {
  return (p.dates || []).filter((d) => Number.isFinite(due(d))).sort((a, b) => due(a) - due(b));
}

// The date a paper's writing window ends at, for its label and its zone.
function writingTarget(p) {
  return p.writing ? (p.dates || []).find((d) => d.id === p.writing.date_id) || null : null;
}

// "16 Oct 2026", the day an instant falls on in a date's own zone.
function venueDay(instant, date) {
  return dayText(partsIn(instant, date?.tz || "AoE"));
}

// A paper is finished when it was decided against or every date has passed.
function isFinished(p, now = Date.now()) {
  if (CLOSED.has(p.outcome)) return true;
  const ds = p.dates || [];
  return ds.length > 0 && ds.every((d) => due(d) < now);
}

function venueOf(p) {
  return [p.conference, p.year].filter(Boolean).join(" ");
}

// --- The "I need N days" field ---------------------------------------------

// The usual writing time, saved to PATCH /api/auth/me while it is typed
// (debounced), with a quiet "Saved". `onPreview` gets every valid value at
// once, `onSaved` the value the server took. `flush()` saves what is pending.
export function writingDaysField({ value, onPreview = null, onSaved = null } = {}) {
  const input = h("input", {
    type: "number", class: "form-control tl-days-input", min: "1", max: "365", step: "1",
    inputmode: "numeric", value: String(value || 30),
  });
  const status = h("span", { class: "tl-saved", role: "status" });
  let saved = Number(value) || 30;
  let fade = null;

  const parse = () => {
    const raw = input.value.trim();
    const v = Number(raw);
    return raw && Number.isInteger(v) && v >= 1 && v <= 365 ? v : null;
  };
  const say = (text, error = false) => {
    clearTimeout(fade);
    status.textContent = text;
    status.classList.toggle("is-error", error);
    status.classList.toggle("is-shown", Boolean(text));
    if (text && !error) fade = setTimeout(() => status.classList.remove("is-shown"), 2500);
  };

  async function save() {
    const v = parse();
    if (v === null || v === saved) return;
    try {
      const me = await api.patch("/api/auth/me", { writing_days: v });
      if (session.me) Object.assign(session.me, me && typeof me === "object" ? me : { writing_days: v });
      saved = v;
      if (parse() === v) say("Saved");
      onSaved?.(v);
    } catch (err) {
      say(err.message, true);
    }
  }
  const later = debounce(save, 600);

  input.addEventListener("input", () => {
    const v = parse();
    if (v === null) {
      later.cancel();
      say(input.value.trim() ? "Use 1 to 365 days." : "", true);
      return;
    }
    say("");
    onPreview?.(v);
    later();
  });
  input.addEventListener("change", () => {
    later.cancel();
    save();
  });

  // The input sits inside its label, so its name reads "I need 30 days to write a paper".
  const el = h("div", { class: "tl-days" },
    h("label", { class: "tl-days-label" }, h("span", { text: "I need" }), input, h("span", { text: "days to write a paper" })),
    status);
  return {
    el,
    input,
    flush: () => {
      later.cancel();
      save();
    },
  };
}

// --- The page ----------------------------------------------------------------

export async function render(ctx) {
  setTitle("Timeline");
  if (!ctx.main.querySelector(".tl-page")) clear(ctx.main).append(loading());

  const [data, folders, share] = await Promise.all([fetchTimeline(), api.get("/api/folders"), api.get("/api/timeline/share")]);
  if (!ctx.alive()) return;

  const prefs = loadPrefs();
  if (prefs.folder && !folders.some((f) => f.path === prefs.folder)) prefs.folder = "";
  const state = { data, folders, prefs, share, usual: data.writing_days || session.me?.writing_days || 30 };
  // Bumped by every local change, so a reload that started before it does not undo it.
  let edits = 0;

  const view = chartView({
    prefs,
    papers: () => ({ all: state.data.projects || [], folder: prefs.folder }),
    env: { usual: () => state.usual, refresh, edited: () => { edits += 1; } },
    empty: emptyState,
  });

  async function refresh() {
    const mine = edits;
    try {
      const fresh = await fetchTimeline();
      if (!ctx.alive() || mine !== edits) return;
      state.data = fresh;
      state.usual = fresh.writing_days || state.usual;
      view.paint();
    } catch (err) {
      flashError(err);
    }
  }

  // The usual time moves every window that does not have its own at once.
  function preview(days) {
    edits += 1;
    state.usual = days;
    for (const p of state.data.projects || []) {
      if (p.writing && !p.writing.own) {
        p.writing = { ...p.writing, days, start: new Date(Date.parse(p.writing.end) - days * DAY).toISOString() };
        p.writing_days = days;
      }
    }
    view.paint();
  }

  const days = writingDaysField({ value: state.usual, onPreview: preview, onSaved: () => refresh() });
  onLeave(() => days.flush());

  const folderSummary = h("span", {}, "Folder: ", h("strong", { text: prefs.folder || "All" }));
  const folderMenu = dropdown({
    summary: [folderSummary, icon("triangle-down")],
    label: "Show the papers of one folder",
    align: "right",
    items: () => {
      const pick = (path) => () => {
        prefs.folder = path;
        savePrefs(prefs);
        folderSummary.lastChild.textContent = path || "All";
        view.paint({ toToday: true });
      };
      return [
        { header: "Show the papers of" },
        { text: "All folders", checked: !prefs.folder, onClick: pick("") },
        ...(state.folders.length
          ? state.folders.map((f) => ({ text: f.path, checked: prefs.folder === f.path, onClick: pick(f.path) }))
          : [{ node: h("div", { class: "dropdown-text", text: "No folders yet. Make them on the list's side." }) }]),
      ];
    },
  });

  // "Shared" while a link works, so it is never forgotten.
  const shareBtn = h("button", { type: "button", class: "btn" });
  const paintShare = () => {
    shareBtn.replaceChildren(icon(state.share ? "globe" : "link"), state.share ? "Shared" : "Share");
    shareBtn.title = state.share ? "Anyone with the link can see this timeline" : "Share this timeline with a link";
  };
  paintShare();
  shareBtn.addEventListener("click", () => shareDialog({
    folders: state.folders,
    folderPath: prefs.folder,
    onChange: (s) => {
      state.share = s;
      paintShare();
    },
  }));

  const bar = h("div", { class: "tl-toolbar" },
    days.el,
    h("div", { class: "tl-toolbar-spacer" }),
    view.zoom,
    folderMenu,
    view.finished,
    shareBtn);

  const page = h("div", { class: "container page tl-page" },
    viewTabs("timeline"),
    h("h1", { class: "sr-only", text: "Timeline of your papers" }),
    bar,
    legend(),
    view.host,
    view.tip.el);
  clear(ctx.main).append(page);
  onLeave(view.listen());
  view.paint({ toToday: true });
}

// /timeline/<token>: someone's timeline, read-only and without signing in
// (DESIGN §3, Sharing the timeline). The chart with its zoom and "Show
// finished papers", and nothing else. No links into the papers, no menus.
export async function renderShared(ctx) {
  setTitle("Timeline");
  clear(ctx.main).append(loading());
  let data;
  try {
    data = await api.get(`/api/shared/timeline/${enc(ctx.params[0])}`);
  } catch (err) {
    clear(ctx.main).append(h("div", { class: "container container-sm page" }, err.status === 404
      ? blankslate({
        iconName: "link",
        title: "This link does not work",
        text: "It was replaced by a new one, or the timeline is no longer shared. Ask for a new link.",
      })
      : flashBox(err.message, "error")));
    return;
  }

  const title = data.owner ? `${data.owner}'s timeline` : "Shared timeline";
  setTitle(title);
  const view = chartView({
    prefs: loadPrefs(),
    papers: () => ({ all: data.projects || [], folder: "" }),
    env: { shared: true },
    empty: () => h("div", { class: "box" }, blankslate({
      iconName: "calendar",
      title: "Nothing on this timeline yet",
      text: "The papers show up here once they are added.",
    })),
  });
  clear(ctx.main).append(h("div", { class: "container page tl-page" },
    h("div", { class: "page-head" },
      h("div", {},
        h("h1", { text: title }),
        h("p", { class: "page-lede", text: "Papers with their deadlines, writing time and conferences. Read-only, and up to date every time you open it." }))),
    h("div", { class: "tl-toolbar" }, h("div", { class: "tl-toolbar-spacer" }), view.zoom, view.finished),
    legend(),
    view.host,
    view.tip.el));
  view.listen();
  view.paint({ toToday: true });
}

// The zoom, "Show finished papers" and the chart, for your own timeline and a
// shared one. `papers()` gives every paper and the folder in view ("" for
// all). `env` is what the rows need besides the hover card.
function chartView({ prefs, papers, env, empty }) {
  let axis = null;
  const tip = tipper();
  const rowEnv = { ...env, tip };
  const host = h("div", { class: "tl-host" });
  const finishedCount = h("span", { class: "color-muted" });

  const zoomButtons = Object.entries(ZOOMS).map(([key, z]) => h("button", {
    type: "button", "aria-pressed": String(key === prefs.zoom), text: z.text,
    onclick: () => {
      prefs.zoom = key;
      savePrefs(prefs);
      zoomButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === z.text)));
      paint({ toToday: true });
    },
  }));
  const zoom = h("div", { class: "segmented", role: "group", "aria-label": "Zoom" }, zoomButtons);

  const finished = h("input", { type: "checkbox", checked: prefs.finished });
  finished.addEventListener("change", () => {
    prefs.finished = finished.checked;
    savePrefs(prefs);
    paint();
  });

  function shownRows() {
    const { all, folder } = papers();
    const inFolder = folder ? all.filter((p) => p.folder_path === folder || (p.folder_path || "").startsWith(`${folder}/`)) : all;
    const now = Date.now();
    const rows = prefs.finished ? inFolder : inFolder.filter((p) => !isFinished(p, now));
    return { all, folder, rows, hidden: inFolder.filter((p) => isFinished(p, now)).length };
  }

  function paint({ toToday = false } = {}) {
    tip.hide();
    const old = host.querySelector(".tl-scroll");
    const keep = !toToday && old && axis
      ? { day: axis.lo + old.scrollLeft / axis.ppd, top: old.scrollTop }
      : null;
    const { all, folder, rows, hidden } = shownRows();
    finishedCount.textContent = hidden ? ` (${hidden})` : "";

    if (!all.length) {
      axis = null;
      clear(host).append(empty());
      return;
    }
    if (!rows.length) {
      axis = null;
      clear(host).append(h("div", { class: "box" }, blankslate({
        iconName: "search",
        title: "No papers to show",
        text: hidden
          ? `${plural(hidden, "finished paper")} ${hidden === 1 ? "is" : "are"} hidden. Turn on Show finished papers${folder ? ", or choose another folder" : ""}.`
          : "No paper in this folder is on the timeline. Choose another folder.",
      })));
      return;
    }

    axis = makeAxis(rows, prefs.zoom);
    const chart = buildChart(rows, axis, rowEnv);
    clear(host).append(chart);
    const name = chart.querySelector(".tl-corner")?.offsetWidth || 0;
    if (keep) {
      chart.scrollLeft = (keep.day - axis.lo) * axis.ppd;
      chart.scrollTop = keep.top;
    } else {
      const view = chart.clientWidth - name;
      chart.scrollLeft = Math.max(0, axis.x(axis.now) - Math.min(160, Math.max(24, view * 0.15)));
    }
  }

  // Scrolling moves what a hover card or a row menu points at. Returns what
  // undoes it when the page is left.
  function listen() {
    const onScroll = () => {
      tip.hide();
      document.querySelectorAll(".tl-pop:popover-open").forEach((pop) => pop.place?.());
    };
    const onKey = (e) => { if (e.key === "Escape") tip.hide(); };
    document.addEventListener("scroll", onScroll, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("keydown", onKey);
      tip.hide();
      document.querySelectorAll(".tl-pop:popover-open").forEach((pop) => pop.hidePopover());
    };
  }

  return {
    tip,
    host,
    zoom,
    finished: h("label", { class: "tl-check" }, finished, "Show finished papers", finishedCount),
    paint,
    listen,
  };
}

function fetchTimeline() {
  return api.get("/api/timeline", { query: { archived: "false" } });
}

function emptyState() {
  return h("div", { class: "box" }, blankslate({
    iconName: "calendar",
    title: "Nothing on the timeline yet",
    text: "Every paper and every plan gets a row here, with its writing time, its deadlines and the conference. Add a paper you are writing, or plan a venue you want to hand something in to before the topic is clear.",
    action: h("div", { class: "flex flex-wrap tl-empty-actions" },
      h("a", { class: "btn btn-primary", href: "/new" }, icon("book"), "New paper"),
      h("a", { class: "btn", href: "/new?plan=1" }, icon("calendar"), "Plan a venue")),
  }));
}

function legend() {
  const item = (cls, text) => h("span", { class: "tl-legend-item" }, h("span", { class: `tl-swatch ${cls}` }), text);
  return h("div", { class: "tl-legend" },
    item("tl-sw-writing", "Writing"),
    item("tl-sw-review", "Review"),
    item("tl-sw-rebuttal", "Rebuttal"),
    item("tl-sw-camera", "Camera-ready"),
    item("tl-sw-conference", "Conference"),
    item("tl-sw-planned", "Planned"),
    h("span", { class: "tl-legend-item" }, h("span", { class: "tl-swatch-mark" }), "Deadline"),
    h("span", { class: "tl-legend-item" }, h("span", { class: "tl-swatch-today" }), "Today"));
}

// --- The axis ------------------------------------------------------------------

// Day 0 is today in the reader's zone. `x(instant)` is the pixel on the track,
// `cal(k)` the calendar day k as a Date whose UTC fields are that day.
function makeAxis(rows, zoomKey) {
  const zoom = ZOOMS[zoomKey];
  const now = Date.now();
  const p = partsIn(now, myTimezone());
  const origin = now - (p.h * 60 + p.mi) * 60000 - (now % 60000);
  const cal0 = Date.UTC(p.y, p.mo, p.d);
  const dayOf = (t) => (t - origin) / DAY;
  const cal = (k) => new Date(cal0 + k * DAY);
  const index = (y, m, d) => Math.round((Date.UTC(y, m, d) - cal0) / DAY);

  let lo = -zoom.before;
  let hi = zoom.after;
  for (const r of rows) {
    const times = sortedDates(r).map(due);
    if (r.writing) times.push(Date.parse(r.writing.start), Date.parse(r.writing.end));
    for (const t of times) {
      if (!Number.isFinite(t)) continue;
      lo = Math.min(lo, Math.floor(dayOf(t)) - 7);
      hi = Math.max(hi, Math.ceil(dayOf(t)) + 21);
    }
  }
  lo = Math.max(lo, -3 * 366);
  hi = Math.min(hi, 6 * 366);
  hi = Math.max(hi, lo + Math.ceil(MIN_WIDTH / zoom.ppd));

  // Start and end on whole weeks, months or quarters.
  if (zoomKey === "weeks") {
    lo -= (cal(lo).getUTCDay() + 6) % 7;
    hi += (7 - ((cal(hi).getUTCDay() + 6) % 7)) % 7;
  } else {
    const step = zoomKey === "quarters" ? 3 : 1;
    const a = cal(lo);
    lo = index(a.getUTCFullYear(), a.getUTCMonth() - (a.getUTCMonth() % step), 1);
    const b = cal(hi);
    const first = b.getUTCDate() === 1 && b.getUTCMonth() % step === 0;
    if (!first) hi = index(b.getUTCFullYear(), b.getUTCMonth() - (b.getUTCMonth() % step) + step, 1);
  }

  return {
    zoomKey,
    ppd: zoom.ppd,
    lo,
    hi,
    now,
    width: (hi - lo) * zoom.ppd,
    cal,
    index,
    dayOf,
    x: (t) => (dayOf(t) - lo) * zoom.ppd,
    xDay: (k) => (k - lo) * zoom.ppd,
  };
}

// Calendar periods (months, quarters or years) between lo and hi, clipped.
function periods(ax, months) {
  const out = [];
  const a = ax.cal(ax.lo);
  let y = a.getUTCFullYear();
  let m = a.getUTCMonth() - (a.getUTCMonth() % months);
  for (;;) {
    const from = ax.index(y, m, 1);
    if (from >= ax.hi) break;
    const to = ax.index(y, m + months, 1);
    const real = new Date(Date.UTC(y, m, 1));
    out.push({ from: Math.max(from, ax.lo), to: Math.min(to, ax.hi), y: real.getUTCFullYear(), m: real.getUTCMonth(), starts: from >= ax.lo });
    m += months;
  }
  return out;
}

function axisHead(ax) {
  const top = h("div", { class: "tl-axis-top" });
  const bottom = h("div", { class: "tl-axis-bottom" });
  const cell = (row, from, to, text, cls = "") => row.append(h("div", {
    class: `tl-axis-cell ${cls}`.trim(), style: { left: px(ax.xDay(from)), width: px((to - from) * ax.ppd) },
  }, h("span", { text })));

  if (ax.zoomKey === "weeks") {
    periods(ax, 1).forEach((q) => cell(top, q.from, q.to, `${MONTH_NAMES[q.m]} ${q.y}`));
    for (let k = ax.lo; k < ax.hi; k++) {
      const c = ax.cal(k);
      const wd = c.getUTCDay();
      cell(bottom, k, k + 1, String(c.getUTCDate()),
        ["tl-axis-day", wd === 0 || wd === 6 ? "is-weekend" : "", wd === 1 ? "is-monday" : "", k === 0 ? "is-today" : ""].filter(Boolean).join(" "));
    }
  } else if (ax.zoomKey === "months") {
    periods(ax, 12).forEach((q) => cell(top, q.from, q.to, String(q.y)));
    periods(ax, 1).forEach((q) => cell(bottom, q.from, q.to, MONTH_NAMES[q.m]));
  } else {
    periods(ax, 3).forEach((q) => cell(top, q.from, q.to, `Q${Math.floor(q.m / 3) + 1} ${q.y}`));
    periods(ax, 1).forEach((q) => cell(bottom, q.from, q.to, MONTHS[q.m], q.m % 3 === 0 ? "is-strong" : ""));
  }
  return h("div", { class: "tl-axis", style: { width: px(ax.width) } },
    top, bottom, h("div", { class: "tl-axis-now", style: { left: px(ax.x(ax.now)) }, title: "Today" }));
}

// Weekends and period lines behind the rows.
function gridLayer(ax) {
  const grid = h("div", { class: "tl-grid", style: { width: px(ax.width) }, "aria-hidden": "true" });
  const line = (k, strong = false) => grid.append(h("div", { class: `tl-gl${strong ? " is-strong" : ""}`, style: { left: px(ax.xDay(k)) } }));
  if (ax.zoomKey === "weeks") {
    for (let k = ax.lo; k < ax.hi; k++) {
      const wd = ax.cal(k).getUTCDay();
      if (wd === 6) grid.append(h("div", { class: "tl-we", style: { left: px(ax.xDay(k)), width: px(Math.min(2, ax.hi - k) * ax.ppd) } }));
      if (wd === 0 && k === ax.lo) grid.append(h("div", { class: "tl-we", style: { left: px(ax.xDay(k)), width: px(ax.ppd) } }));
      if (wd === 1) line(k, ax.cal(k).getUTCDate() <= 7);
    }
  } else if (ax.zoomKey === "months") {
    periods(ax, 1).filter((q) => q.starts).forEach((q) => line(q.from, q.m === 0));
  } else {
    periods(ax, 1).filter((q) => q.starts).forEach((q) => line(q.from, q.m % 3 === 0));
  }
  return grid;
}

// --- The chart -----------------------------------------------------------------

function buildChart(rows, ax, env) {
  const canvas = h("div", { class: `tl-canvas tl-zoom-${ax.zoomKey}` },
    gridLayer(ax),
    h("div", { class: "tl-top" },
      h("div", { class: "tl-head" },
        h("div", { class: "tl-corner tl-corner-head", text: plural(rows.length, "paper") }),
        axisHead(ax)),
      loadRow(rows, ax, env)),
    rows.map((p) => rowEl(p, ax, env)),
    h("div", { class: "tl-now", style: { left: `calc(var(--tl-name) + ${px(ax.x(ax.now))})` }, "aria-hidden": "true" }));
  return h("div", { class: "tl-scroll", role: "region", "aria-label": "Timeline", tabindex: "-1" }, canvas);
}

function rowEl(p, ax, env) {
  const track = h("div", { class: "tl-track", style: { width: px(ax.width) } });
  for (const span of spansOf(p)) track.append(barEl(span, p, ax, env));
  const ds = sortedDates(p);
  for (const d of ds) track.append(markEl(d, p, ax, env));
  if (!ds.length && !p.writing) {
    track.append(h("span", { class: "tl-empty-row", style: { left: px(ax.x(ax.now) + 12) }, text: "No dates yet" }));
  }
  const cls = ["tl-row", p.planned ? "is-planned" : "", isFinished(p) ? "is-finished" : ""].filter(Boolean).join(" ");
  return h("div", { class: cls, dataset: { project: p.id, name: p.name } }, nameCell(p, env), track);
}

function nameCell(p, env) {
  const venue = venueOf(p);
  const sub = h("div", { class: "tl-name-sub" },
    p.planned ? plannedLabel() : null,
    outcomeLabel(p.outcome),
    p.title ? h("span", { class: "tl-name-title truncate", title: p.title, text: p.title }) : null);
  // A shared timeline names the paper without a way into it.
  const name = env.shared
    ? h("span", { class: "tl-name-link truncate", title: p.name, text: p.name })
    : h("a", { class: "tl-name-link truncate", href: `/p/${enc(p.name)}`, title: p.name, text: p.name });
  return h("div", { class: "tl-name" },
    h("div", { class: "tl-name-text" },
      name,
      sub.childNodes.length ? sub : null,
      venue ? h("div", { class: "tl-venue truncate", text: [venue, p.track].filter(Boolean).join(" · ") }) : null),
    env.shared ? null : rowMenu(p, env));
}

// The lighter bars around the writing window: hand-in to notification,
// the rebuttal window, notification to camera-ready (accepted or pending
// only) and the conference.
function spansOf(p) {
  const ds = sortedDates(p);
  const of = (kind) => ds.filter((d) => d.kind === kind);
  const out = [];
  const target = writingTarget(p);
  if (p.writing) out.push({ kind: "writing", from: Date.parse(p.writing.start), to: Date.parse(p.writing.end), toDate: target });

  const subs = of("submission");
  const handIn = target || subs[subs.length - 1] || null;
  const notes = of("notification");
  if (handIn) {
    const note = notes.find((n) => due(n) > due(handIn));
    if (note) out.push({ kind: "review", from: due(handIn), to: due(note), fromDate: handIn, toDate: note });
  }
  const rs = of("rebuttal_start")[0];
  const re = of("rebuttal_end").find((d) => !rs || due(d) > due(rs));
  if (rs && re) out.push({ kind: "rebuttal", from: due(rs), to: due(re), fromDate: rs, toDate: re });

  const cr = of("camera_ready")[0];
  if (cr && (p.outcome === "accepted" || !p.outcome)) {
    const before = notes.filter((n) => due(n) < due(cr));
    const from = before[before.length - 1];
    if (from) out.push({ kind: "camera", from: due(from), to: due(cr), fromDate: from, toDate: cr });
  }

  const cs = of("conference_start")[0];
  const ce = of("conference_end").find((d) => !cs || due(d) >= due(cs));
  const first = cs || ce;
  if (first) {
    // A day the venue gave is stored as its last second, the conference starts at its first.
    const from = first.date_only ? due(first) - DAY + 1000 : due(first);
    const to = ce ? due(ce) : first.date_only ? due(first) : due(first) + DAY;
    out.push({ kind: "conference", from, to, fromDate: first, toDate: ce || first });
  }
  return out.filter((s) => Number.isFinite(s.from) && Number.isFinite(s.to) && s.to > s.from);
}

function barEl(span, p, ax, env) {
  const left = ax.x(span.from);
  const width = Math.max(ax.x(span.to) - left, 3);
  const text = span.kind === "writing"
    ? `Writing, ${plural(p.writing.days, "day")}`
    : span.kind === "conference" && p.conference ? p.conference : BAR_TEXT[span.kind];
  const label = h("span", { class: "tl-bar-text", text });
  const el = h("div", {
    class: `tl-bar tl-bar-${span.kind}`,
    style: { left: px(left), width: px(width) },
  }, label);
  // A deadline inside the bar (the abstract before the paper, say) draws its
  // marker over it. The text starts after the last such marker, and goes when
  // no room is left. The hover card still says everything.
  const inside = (p.dates || [])
    .map((d) => ax.x(Date.parse(d.due_at)) - left)
    .filter((x) => x > 6 && x < width - 6);
  if (inside.length) {
    const start = Math.max(...inside) + 12;
    if (width - start < 56) label.hidden = true;
    else label.style.marginLeft = px(start - 8);
  }
  if (span.to <= ax.now) {
    el.classList.add("is-past");
  } else if (span.from < ax.now) {
    el.classList.add("is-partly");
    el.style.setProperty("--past", `${((ax.now - span.from) / (span.to - span.from)) * 100}%`);
  }
  if (span.kind === "writing") {
    // The writing window carries the start date, so it is reachable by keyboard.
    el.tabIndex = 0;
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", `${p.name}, start writing by ${venueDay(span.from, span.toDate)}, ${plural(p.writing.days, "day")} before ${span.toDate ? dateLabel(span.toDate) : "the deadline"}`);
  }
  env.tip.attach(el, () => barTip(span, p, env));
  return el;
}

function barTip(span, p, env) {
  const name = h("div", { class: "tl-tip-paper", text: p.name });
  if (span.kind === "writing") {
    const w = p.writing;
    const target = span.toDate;
    const started = span.from <= Date.now();
    const whose = w.own ? "this paper's own time" : env.shared ? "the usual time" : "your usual time";
    return [
      h("div", { class: "tl-tip-title" }, h("strong", { text: `Writing, ${plural(w.days, "day")}` }),
        h("span", { class: "color-muted", text: whose })),
      h("div", { class: "tl-tip-when" }, `${started ? "Started" : "Start"} by ${venueDay(span.from, target)}`),
      h("div", {}, relative(secondsUntil(w.start))),
      target ? h("div", { class: "tl-tip-muted" }, `Hand in by ${formatDue(target)} (${dateLabel(target)})`) : null,
      name,
    ];
  }
  const range = span.fromDate && span.toDate && span.fromDate !== span.toDate
    ? `${formatDue(span.fromDate)} to ${formatDue(span.toDate)}`
    : formatDue(span.toDate || span.fromDate);
  return [
    h("div", { class: "tl-tip-title" }, h("strong", { text: span.kind === "conference" ? venueOf(p) || "Conference" : BAR_TEXT[span.kind] })),
    h("div", { class: "tl-tip-when", text: range }),
    name,
  ];
}

function markEl(d, p, ax, env) {
  const passed = due(d) < ax.now;
  const answer = d.answer;
  const cls = ["tl-mark", DEADLINES.has(d.kind) ? "is-deadline" : "is-event"];
  if (passed) cls.push("is-past");
  if (answer === "yes" || answer === "accepted") cls.push("is-done");
  if (answer === "no" || answer === "rejected") cls.push("is-missed");
  // Red under 3 days and amber under 14, like the countdowns.
  if (!passed && !answer) cls.push(`is-${urgency(secondsUntil(d.due_at))}`);
  if (p.next_date?.id === d.id) cls.push("is-next");
  const el = h("button", {
    type: "button",
    class: cls.join(" "),
    style: { left: px(ax.x(due(d))) },
    dataset: { date: d.id, kind: d.kind },
    "aria-label": `${p.name}, ${dateLabel(d)}, ${formatDue(d)}, ${relative(secondsUntil(d.due_at))}`,
  });
  env.tip.attach(el, () => markTip(d, p));
  return el;
}

function markTip(d, p) {
  const answered = ANSWERED[d.answer];
  const moves = d.changes || [];
  const last = moves[moves.length - 1];
  return [
    h("div", { class: "tl-tip-title" }, h("strong", { text: dateLabel(d) }),
      p.next_date?.id === d.id ? h("span", { class: "Label label-accent", text: "Next" }) : null,
      answered ? h("span", { class: `Label ${answered[1]}`, text: answered[0] }) : null),
    h("div", { class: "tl-tip-when", text: formatDue(d) }),
    h("div", {}, countdown(d.due_at)),
    last?.from
      ? h("div", { class: "tl-tip-moved" }, icon("history", { size: 12 }),
        ` Moved from ${formatShort(last.from, d.tz, { compareTo: d.due_at, dateOnly: d.date_only })}`,
        moves.length > 1 ? ` (${moves.length} moves)` : "")
      : null,
    h("div", { class: "tl-tip-muted", text: `In your time zone this is ${formatInMyZone(d.due_at)}.` }),
    h("div", { class: "tl-tip-paper", text: p.name }),
  ];
}

// --- The writing load strip --------------------------------------------------

// Runs of days (Weeks) or weeks (coarser zooms) with the same writing windows.
// A week counts the most windows that overlap on one of its days.
function loadSegments(rows, ax) {
  const wins = rows.filter((p) => p.writing).map((p) => ({
    p, s: ax.dayOf(Date.parse(p.writing.start)), e: ax.dayOf(Date.parse(p.writing.end)),
  })).filter((w) => Number.isFinite(w.s) && Number.isFinite(w.e));
  const daily = [];
  for (let k = ax.lo; k < ax.hi; k++) daily.push(wins.filter((w) => w.s < k + 1 && w.e > k));

  const buckets = [];
  if (ax.zoomKey === "weeks") {
    daily.forEach((set, i) => buckets.push({ from: ax.lo + i, to: ax.lo + i + 1, count: set.length, set }));
  } else {
    let k = ax.lo;
    while (k < ax.hi) {
      const end = Math.min(ax.hi, k + (7 - ((ax.cal(k).getUTCDay() + 6) % 7)));
      const days = daily.slice(k - ax.lo, end - ax.lo);
      buckets.push({ from: k, to: end, count: Math.max(0, ...days.map((d) => d.length)), set: [...new Set(days.flat())] });
      k = end;
    }
  }
  const segs = [];
  for (const b of buckets) {
    const key = `${b.count}|${b.set.map((w) => w.p.id).sort().join(",")}`;
    const last = segs[segs.length - 1];
    if (last && last.key === key && last.to === b.from) last.to = b.to;
    else segs.push({ ...b, key });
  }
  return segs.filter((s) => s.count > 0);
}

function loadRow(rows, ax, env) {
  const strip = h("div", { class: "tl-load", style: { width: px(ax.width) } });
  for (const seg of loadSegments(rows, ax)) {
    const level = seg.count >= 3 ? "many" : seg.count === 2 ? "two" : "one";
    const from = dayText({ y: ax.cal(seg.from).getUTCFullYear(), mo: ax.cal(seg.from).getUTCMonth(), d: ax.cal(seg.from).getUTCDate() });
    const lastDay = ax.cal(seg.to - 1);
    const to = dayText({ y: lastDay.getUTCFullYear(), mo: lastDay.getUTCMonth(), d: lastDay.getUTCDate() });
    const when = from === to ? `on ${from}` : `from ${from} to ${to}`;
    const names = seg.set.map((w) => w.p.name);
    const el = h("div", {
      class: `tl-load-seg is-${level}${seg.to <= 0 ? " is-past" : ""}`,
      style: { left: px(ax.xDay(seg.from)), width: px((seg.to - seg.from) * ax.ppd) },
      dataset: { count: String(seg.count) },
      tabindex: "0",
      role: "img",
      "aria-label": `${seg.count === 1 ? "1 writing window" : `${seg.count} writing windows at once`} ${when}: ${names.join(", ")}`,
    }, h("span", { text: String(seg.count) }));
    env.tip.attach(el, () => [
      h("div", { class: "tl-tip-title" }, h("strong", {
        text: seg.count === 1 ? "1 writing window" : `${seg.count} writing windows at once`,
      })),
      h("div", { class: "tl-tip-when", text: when[0].toUpperCase() + when.slice(1) }),
      h("ul", { class: "tl-tip-list" }, seg.set.map((w) => {
        const target = writingTarget(w.p);
        return h("li", {}, h("strong", { text: w.p.name }),
          h("span", { class: "color-muted", text: ` until ${target ? formatDue(target) : venueDay(Date.parse(w.p.writing.end), null)}` }));
      })),
    ]);
    strip.append(el);
  }
  strip.append(h("div", { class: "tl-load-now", style: { left: px(ax.x(ax.now)) }, "aria-hidden": "true" }));
  return h("div", { class: "tl-loadrow" },
    h("div", { class: "tl-corner tl-corner-load" }, "Writing load"),
    strip);
}

// --- Hover cards -----------------------------------------------------------------

// A focused marker, bar or load segment is scrolled out from under the sticky
// name column and axis. scroll-padding would do it for them, but it also
// scrolls the chart back to its start whenever a name in the sticky column
// takes focus.
function reveal(target) {
  const box = target.closest(".tl-scroll");
  if (!box) return;
  const outer = box.getBoundingClientRect();
  const r = target.getBoundingClientRect();
  const name = box.querySelector(".tl-corner")?.offsetWidth || 0;
  const left = outer.left + name + 16;
  const right = outer.right - 16;
  if (r.left < left) box.scrollLeft -= left - r.left;
  else if (r.right > right) box.scrollLeft += Math.min(r.right - right, r.left - left);
  if (target.closest(".tl-track")) {
    const top = outer.top + (box.querySelector(".tl-top")?.offsetHeight || 0) + 4;
    if (r.top < top) box.scrollTop -= top - r.top;
    else if (r.bottom > outer.bottom - 4) box.scrollTop += r.bottom - outer.bottom + 4;
  }
}

// One card for the whole chart, fixed to the viewport so the chart's scrolling
// box does not clip it. It shows on hover and on keyboard focus.
function tipper() {
  const el = h("div", { class: "tl-tip", id: nextId("tl-tip"), role: "tooltip", hidden: true });
  let owner = null;

  function show(target, build) {
    owner?.removeAttribute("aria-describedby");
    owner = target;
    clear(el).append(...build().filter(Boolean));
    el.hidden = false;
    const r = target.getBoundingClientRect();
    const w = el.offsetWidth;
    const ht = el.offsetHeight;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    let top = r.bottom + 8;
    if (top + ht > window.innerHeight - 8) top = Math.max(8, r.top - ht - 8);
    el.style.left = px(left);
    el.style.top = px(top);
    target.setAttribute("aria-describedby", el.id);
  }

  function hide(target = null) {
    if (target && target !== owner) return;
    el.hidden = true;
    owner?.removeAttribute("aria-describedby");
    owner = null;
  }

  function attach(target, build) {
    target.addEventListener("mouseenter", () => show(target, build));
    target.addEventListener("mouseleave", () => { if (document.activeElement !== target) hide(target); });
    target.addEventListener("focus", () => {
      reveal(target);
      show(target, build);
    });
    target.addEventListener("blur", () => hide(target));
  }

  return { el, attach, hide: () => hide() };
}

// --- A row's menu with this paper's writing time ------------------------------------

// A popover in the top layer, so the chart's scrolling box does not clip it.
function rowMenu(p, env) {
  const id = nextId("tl-menu");
  const btn = h("button", {
    type: "button", class: "btn-octicon tl-kebab", popovertarget: id,
    "aria-label": `Actions for ${p.name}`, title: "Writing time and more",
  }, icon("kebab"));
  const pop = h("div", { id, popover: "auto", class: "tl-pop", role: "dialog", "aria-label": `Writing time for ${p.name}` });

  pop.place = () => {
    const r = btn.getBoundingClientRect();
    const w = pop.offsetWidth;
    const ht = pop.offsetHeight;
    const left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    let top = r.bottom + 4;
    if (top + ht > window.innerHeight - 8) top = Math.max(8, r.top - ht - 4);
    pop.style.left = px(left);
    pop.style.top = px(top);
  };

  function fill() {
    const usual = env.usual();
    const own = p.own_writing_days != null;
    const inputId = nextId("tl-own");
    const input = h("input", {
      type: "number", id: inputId, class: "form-control form-control-sm tl-days-input", min: "1", max: "365", step: "1",
      inputmode: "numeric", placeholder: String(usual), value: own ? String(p.own_writing_days) : "", disabled: !canEdit(p),
    });
    const error = h("div", { class: "form-error", role: "alert", hidden: true });
    const save = h("button", { type: "submit", class: "btn btn-sm btn-primary", text: "Save", disabled: !canEdit(p) });
    const form = h("form", { class: "tl-pop-form" },
      h("label", { for: inputId, text: "Writing time for this paper" }),
      h("div", { class: "tl-pop-row" }, input, h("span", { text: "days" }), h("span", { class: "flex-1" }), save),
      h("div", { class: "form-hint", text: canEdit(p) ? `Leave it empty for your usual ${plural(usual, "day")}.` : "Only the owner and editors can change it." }),
      error);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const raw = input.value.trim();
      const v = Number(raw);
      if (raw && !(Number.isInteger(v) && v >= 1 && v <= 365)) {
        error.textContent = "Use 1 to 365 days, or leave it empty.";
        error.hidden = false;
        input.focus();
        return;
      }
      try {
        await busy(save, () => api.patch(projectUrl(p.id), { writing_days: raw ? v : null }));
        env.edited();
        pop.hidePopover();
        flash(raw ? `${p.name} has its own writing time now, ${plural(v, "day")}.` : `${p.name} uses your usual writing time again.`, "success");
        await env.refresh();
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    });
    clear(pop).append(form,
      h("div", { class: "tl-pop-foot" },
        h("a", { class: "tl-pop-link", href: `/p/${enc(p.name)}`, onclick: () => pop.hidePopover() }, icon("book"), "Open the paper")));
    return input;
  }

  pop.addEventListener("toggle", (e) => {
    if (e.newState !== "open") return;
    const input = fill();
    pop.place();
    if (!input.disabled) input.focus();
  });
  return h("div", { class: "tl-menu" }, btn, pop);
}

// --- Sharing the timeline ------------------------------------------------------------

// The public, read-only link to your timeline, of every paper or of one folder
// and its subfolders (DESIGN §3, Sharing the timeline). A new link starts with
// the folder the page shows. `onChange` gets the link, or null once it is gone.
async function shareDialog({ folders, folderPath, onChange }) {
  let share;
  try {
    share = await api.get("/api/timeline/share");
  } catch (err) {
    flashError(err);
    return;
  }
  onChange(share);

  const pick = select([["", "All papers"], ...folders.map((f) => [f.id, f.path])],
    share ? share.folder_id || "" : folders.find((f) => f.path === folderPath)?.id || "");
  const saved = h("span", { class: "tl-saved", role: "status" });
  const error = h("div");
  const body = h("div", { class: "tl-share" });
  const d = openDialog({ title: "Share the timeline", body, footer: [] });
  const foot = d.el.querySelector(".dialog-footer");

  const fail = (err) => clear(error).append(h("div", { class: "mt-3" }, flashBox(err.message, "error")));
  const changed = (s) => {
    share = s;
    onChange(s);
    clear(error);
    paint();
  };

  // The folder of a working link changes at once. The link stays the same.
  let fade = null;
  pick.addEventListener("change", async () => {
    if (!share) return;
    clearTimeout(fade);
    saved.classList.remove("is-shown");
    try {
      share = await api.patch("/api/timeline/share", { folder: pick.value || null });
      onChange(share);
      clear(error);
      saved.textContent = "Saved";
      saved.classList.add("is-shown");
      fade = setTimeout(() => saved.classList.remove("is-shown"), 2500);
    } catch (err) {
      fail(err);
    }
  });

  async function make(button) {
    try {
      changed(await busy(button, () => api.post("/api/timeline/share", { folder: pick.value || null })));
    } catch (err) {
      fail(err);
    }
  }

  function paint() {
    const parts = [
      h("p", { class: "color-muted", text: "Anyone with the link sees this chart without signing in: the names, titles and venues of the papers, their dates and their writing time. Not their files, notes, people or anything else in meertex. Archived papers stay out." }),
      field("Papers", pick, {
        hint: h("span", {},
          folders.length ? "A folder shows its subfolders too. " : "Make folders on the list's side to share only some papers. ",
          saved),
      }),
    ];
    if (share) {
      const url = h("input", { type: "text", id: nextId("tl-share-url"), class: "form-control", readonly: true, value: share.url });
      url.addEventListener("focus", () => url.select());
      parts.push(h("div", { class: "form-group" },
        h("label", { for: url.id, text: "Link" }),
        h("div", { class: "copy-field" }, url, copyButton(share.url, { label: "Copy link", cls: "btn", showText: true })),
        h("div", { class: "form-hint" },
          h("a", { href: share.url, target: "_blank", rel: "noopener", text: "Open it" }), " to see what they see.")));

      const stop = h("button", { type: "button", class: "btn btn-danger", text: "Stop sharing" });
      stop.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "Stop sharing the timeline?",
          message: "The link stops working right away. You can make a new one later.",
          confirmText: "Stop sharing",
          danger: true,
        });
        if (!ok) return;
        try {
          await busy(stop, () => api.del("/api/timeline/share"));
          changed(null);
        } catch (err) {
          fail(err);
        }
      });
      const renew = h("button", { type: "button", class: "btn", text: "New link" });
      renew.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "Make a new link?",
          message: "The current link stops working right away. Send the new one to everyone who should still see the timeline.",
          confirmText: "Make a new link",
          danger: true,
        });
        if (ok) make(renew);
      });
      clear(foot).append(stop, h("span", { class: "flex-1" }), renew,
        h("button", { type: "button", class: "btn btn-primary", text: "Done", onclick: () => d.close() }));
    } else {
      const create = h("button", { type: "button", class: "btn btn-primary" }, icon("link"), "Create link");
      create.addEventListener("click", () => make(create));
      clear(foot).append(h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), create);
    }
    clear(body).append(...parts, error);
  }
  paint();
}

// --- On a paper's overview ----------------------------------------------------------

// "Start writing by 16 Oct 2026 (in 12 days), 30 days before Paper submission."
export function writingLine(p) {
  const w = p.writing;
  if (!w) return null;
  const target = writingTarget(p);
  const start = Date.parse(w.start);
  if (!Number.isFinite(start)) return null;
  const started = start <= Date.now();
  return h("div", { class: "tl-writing-line mb-3" },
    icon("pencil", { cls: "color-muted" }),
    h("span", { class: "tl-writing-text" },
      started ? "Writing started on " : "Start writing by ",
      h("strong", { text: venueDay(start, target) }),
      " (", countdown(w.start), `), ${plural(w.days, "day")} before ${target ? dateLabel(target) : "the deadline"}.`),
    h("a", { href: "/timeline", class: "nowrap" }, icon("calendar"), " Timeline"));
}

// The box on a plan's overview, with "Start the paper".
export function planBox(p, ctx) {
  const venue = venueOf(p) || "a venue";
  const start = ctx.canEdit ? h("button", { type: "button", class: "btn btn-primary" }, icon("pencil"), "Start the paper") : null;
  start?.addEventListener("click", () => startPaperDialog(p));
  return h("div", { class: "box tl-plan-box mb-4" },
    h("div", { class: "box-body" },
      h("h3", {}, plannedLabel(), h("span", { text: "No paper yet" })),
      h("p", { text: `This is a plan for ${venue}. Start the paper when the topic is clear.` }),
      start || h("p", { class: "text-small color-muted mb-0", text: "The owner or an editor can start the paper." })));
}

function startPaperDialog(p) {
  const venue = venueOf(p) || "the venue";
  const title = textInput({ value: p.title || "", placeholder: "Green Large Language Models", autocomplete: "off", required: true });
  const paper = textInput({ placeholder: "derived from the title", autocomplete: "off", pattern: "[A-Za-z0-9._\\-]*" });
  const keep = h("input", { type: "checkbox", id: nextId("keep-name") });
  const nameOut = h("span", { class: "name", text: "…" });
  const nameStatus = h("div", { class: "text-small color-muted mt-2" });
  const starter = starterChoice();
  const error = h("div");
  const go = h("button", { type: "button", class: "btn btn-primary", text: "Start the paper" });

  let token = 0;
  const preview = debounce(async () => {
    const mine = ++token;
    nameStatus.textContent = "";
    if (keep.checked) {
      nameOut.textContent = p.name;
      return;
    }
    if (!title.value.trim() && !paper.value.trim()) {
      nameOut.textContent = "…";
      nameStatus.textContent = "Type a title to see the name.";
      return;
    }
    try {
      const res = await api.post("/api/projects", {
        title: title.value.trim(), paper: paper.value.trim(), conference: p.conference || "",
        year: p.year || null, track: p.track || "", dry_run: true, dates: [],
      });
      if (mine === token) nameOut.textContent = res?.name || "…";
    } catch (err) {
      if (mine !== token) return;
      nameOut.textContent = "…";
      nameStatus.textContent = err.message;
    }
  }, 300);
  [title, paper].forEach((el) => el.addEventListener("input", preview));
  keep.addEventListener("change", preview);

  const d = openDialog({
    title: `Start the paper for ${venue}`,
    wide: true,
    body: h("div", { class: "tl-start-form" },
      h("p", { class: "color-muted", text: "The plan becomes a paper. Its venue, dates, notes and people stay. The name is built again from your naming pattern, now with the paper in it." }),
      field(["Title", h("span", { class: "color-danger", text: " *" })], title),
      field("Short name", paper, { optional: true, hint: "The {paper} in your naming pattern." }),
      h("div", { class: "form-group" },
        h("div", { class: "name-preview", "aria-live": "polite" },
          icon("book", { cls: "color-muted" }),
          h("span", { class: "color-muted", text: "The paper will be called" }),
          nameOut),
        nameStatus),
      h("div", { class: "form-checkbox" }, keep,
        h("label", { for: keep.id }, "Keep the name ", h("code", { text: p.name }))),
      h("fieldset", { class: "form-group" }, h("legend", { text: "Start with" }), starter.el),
      error),
    footer: [h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }), go],
  });
  title.focus();
  preview();

  // A template goes in first, while the project is still a plan. A failed
  // upload then leaves the plan as it was, like on the new-paper form. When
  // the start fails after it, a second try does not import again.
  let imported = null;
  go.addEventListener("click", async () => {
    clear(error);
    const fail = (msg) => error.append(h("div", { class: "mt-3" }, flashBox(msg, "error")));
    if (!title.value.trim()) {
      title.classList.add("is-invalid");
      title.focus();
      fail("A paper needs a title.");
      return;
    }
    title.classList.remove("is-invalid");
    const problem = starter.problem();
    if (problem && !imported) {
      fail(problem);
      return;
    }
    const zip = starter.zip();
    try {
      const started = await busy(go, async () => {
        if (zip && !imported) {
          try {
            imported = { result: await api.post(projectUrl(p.id, "/import"), zip, { contentType: "application/zip" }), name: zip.name };
          } catch (err) {
            throw new Error(`The template could not be imported. ${err.message.replace(/\.?$/, ".")} The plan is unchanged.`);
          }
        }
        const body = { title: title.value.trim(), starter: imported ? "empty" : starter.starter(), keep_name: keep.checked };
        if (paper.value.trim()) body.paper = paper.value.trim();
        return api.post(projectUrl(p.id, "/start"), body);
      });
      d.close();
      const base = `/p/${enc(started.name)}`;
      queueFlash(h("div", {},
        imported ? importSummary(imported.result, imported.name) : null,
        h("p", {}, "Started the paper ", h("strong", { text: started.name }), ". ",
          h("a", { href: editorHref(base), text: "Write in the browser" }),
          ", or check it out on your computer:"),
        commandLine(`meertex checkout ${started.name}`)), "success");
      navigate(base, { replace: true });
    } catch (err) {
      fail(err.message);
    }
  });
}
