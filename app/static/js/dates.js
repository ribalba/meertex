// Date kinds (DESIGN §5) and the editor row used wherever dates are entered:
// the new-paper form, the overview's timeline and the resubmit form.

import { h, nextId } from "./dom.js";
import { icon } from "./icons.js";
import { allTimezones, inputValues, isValidTz } from "./time.js";

export const DATE_KINDS = [
  ["abstract", "Abstract registration"],
  ["submission", "Paper submission"],
  ["supplementary", "Supplementary material"],
  ["rebuttal_start", "Rebuttal opens"],
  ["rebuttal_end", "Rebuttal deadline"],
  ["notification", "Notification"],
  ["revision", "Revision due"],
  ["camera_ready", "Camera-ready"],
  ["conference_start", "Conference starts"],
  ["conference_end", "Conference ends"],
  ["other", "Other"],
];
const KIND_LABEL = Object.fromEntries(DATE_KINDS);

export function defaultLabel(kind) {
  return kind === "other" ? "" : KIND_LABEL[kind] || kind;
}

export function dateLabel(date) {
  return date.label || defaultLabel(date.kind) || "Date";
}

export const DATE_ICONS = {
  abstract: "file-text",
  submission: "upload",
  supplementary: "file",
  rebuttal_start: "comment",
  rebuttal_end: "comment",
  notification: "mail",
  revision: "pencil",
  camera_ready: "check-circle",
  conference_start: "people",
  conference_end: "people",
  other: "calendar",
};

// Every zone a CFP may name, in one select. AoE comes first and is the default,
// because most CFPs use it. Then UTC, the fixed offsets, and every IANA zone
// the browser knows, grouped by region. A text field with suggestions looked
// like it only offered AoE: browsers filter the suggestions by what is typed.
const OFFSETS = [
  "UTC-12", "UTC-11", "UTC-10", "UTC-9:30", "UTC-9", "UTC-8", "UTC-7", "UTC-6", "UTC-5", "UTC-4",
  "UTC-3:30", "UTC-3", "UTC-2", "UTC-1", "UTC+1", "UTC+2", "UTC+3", "UTC+3:30", "UTC+4", "UTC+4:30",
  "UTC+5", "UTC+5:30", "UTC+5:45", "UTC+6", "UTC+6:30", "UTC+7", "UTC+8", "UTC+8:45", "UTC+9",
  "UTC+9:30", "UTC+10", "UTC+10:30", "UTC+11", "UTC+12", "UTC+12:45", "UTC+13", "UTC+14",
];

let zoneGroups = null;
function regions() {
  if (!zoneGroups) {
    zoneGroups = new Map();
    // Etc/GMT+5 is five hours *behind* UTC, which nobody expects. The offsets
    // above say the same the right way round.
    for (const zone of allTimezones()) {
      if (!zone.includes("/") || zone.startsWith("Etc/")) continue;
      const region = zone.split("/")[0];
      if (!zoneGroups.has(region)) zoneGroups.set(region, []);
      zoneGroups.get(region).push(zone);
    }
  }
  return zoneGroups;
}

export function tzInput(value = "AoE", attrs = {}) {
  const current = value || "AoE";
  const groups = regions();
  const known = current === "AoE" || current === "UTC" || OFFSETS.includes(current)
    || [...groups.values()].some((zones) => zones.includes(current));
  return h("select", { class: "form-select", ...attrs, value: current },
    // A zone saved before (say "GMT+1") that the list does not have stays selectable.
    known ? null : h("option", { value: current }, current),
    h("option", { value: "AoE", title: "Anywhere on Earth" }, "AoE (UTC-12)"),
    h("option", { value: "UTC" }, "UTC"),
    h("optgroup", { label: "UTC offsets" }, OFFSETS.map((o) => h("option", { value: o }, o))),
    [...groups].map(([region, zones]) => h("optgroup", { label: region },
      zones.map((z) => h("option", { value: z }, z.replaceAll("_", " "))))));
}

// A date row: kind, label, day, optional time and zone. A row without a time
// is a date the venue gave as a day (`date`), with a time it is `due_at`
// local to the zone. `date` is an existing date when editing one.
export function dateRow(template = null, { onRemove = null, kinds = null, fixedKind = null } = {}) {
  // A template with a due_at is an existing date (or a copy of one). One
  // without is only a starting kind for a new row.
  const timed = template?.due_at ? template : null;
  const date = template?.id ? template : null;
  const initial = timed ? { ...inputValues(timed), tz: timed.tz || "AoE" } : { date: "", time: "", tz: template?.tz || "AoE" };
  const ids = { kind: nextId("dk"), label: nextId("dl"), date: nextId("dd"), time: nextId("dt"), tz: nextId("dz") };

  const kindOptions = (kinds || DATE_KINDS).map(([k, l]) => h("option", { value: k }, l));
  const kind = h("select", { class: "form-select", id: ids.kind, value: fixedKind || template?.kind || "submission", disabled: Boolean(fixedKind) }, kindOptions);
  const startLabel = template?.label && template.label !== defaultLabel(template.kind) ? template.label : "";
  const label = h("input", { type: "text", class: "form-control", id: ids.label, value: startLabel, maxlength: "200" });
  const day = h("input", { type: "date", class: "form-control", id: ids.date, value: initial.date, required: true });
  const time = h("input", { type: "time", class: "form-control", id: ids.time, value: initial.time });
  const tz = tzInput(initial.tz, { id: ids.tz });
  const error = h("div", { class: "form-error date-row-error", hidden: true, role: "alert" });

  const syncPlaceholder = () => {
    label.placeholder = kind.value === "other" ? "Required for Other" : defaultLabel(kind.value);
  };
  kind.addEventListener("change", syncPlaceholder);
  syncPlaceholder();

  const group = (cls, id, text, input) => h("div", { class: `form-group ${cls}` }, h("label", { for: id, text }), input);
  const el = h("div", { class: "date-row" },
    group("date-kind", ids.kind, "Kind", kind),
    group("date-label", ids.label, "Label", label),
    group("date-day", ids.date, "Day", day),
    group("date-time", ids.time, "Time (optional)", time),
    group("date-tz", ids.tz, "Time zone", tz),
    onRemove
      ? h("button", { type: "button", class: "btn-octicon danger date-remove", "aria-label": "Remove this date", title: "Remove", onclick: () => onRemove(el) }, icon("trash"))
      : null,
    error,
  );

  function showError(msg) {
    error.textContent = msg || "";
    error.hidden = !msg;
  }

  // Null with the reason shown when the row is not usable.
  function value() {
    showError("");
    if (!day.value) { showError("Pick a day."); day.focus(); return null; }
    if (kind.value === "other" && !label.value.trim()) { showError("A date of kind Other needs a label."); label.focus(); return null; }
    const zone = tz.value || "AoE";
    if (!isValidTz(zone)) { showError(`"${zone}" is not a time zone. Use AoE, UTC, UTC+2 or a name like Europe/Berlin.`); tz.focus(); return null; }
    const out = { kind: kind.value, tz: zone };
    if (label.value.trim()) out.label = label.value.trim();
    else if (date) out.label = "";
    if (time.value) out.due_at = `${day.value}T${time.value}`;
    else out.date = day.value;
    return out;
  }

  // Only what changed, so an edit to the label alone is not recorded as a move.
  function changes() {
    const v = value();
    if (!v || !date) return v;
    const out = {};
    if (v.kind !== date.kind) out.kind = v.kind;
    if ((v.label || "") !== (startLabel || "")) out.label = v.label || "";
    const moved = day.value !== initial.date || time.value !== initial.time || v.tz !== initial.tz;
    if (moved) {
      out.tz = v.tz;
      if (v.due_at) out.due_at = v.due_at;
      else out.date = v.date;
    }
    return out;
  }

  return { el, value, changes, showError, focus: () => day.focus() };
}

// A list of date rows with "Add a date", for forms that create several dates.
export function dateList(initial = []) {
  const rows = [];
  const container = h("div", { class: "date-rows" });
  const empty = h("div", { class: "date-rows-empty", text: "No dates yet. Add the deadlines from the call for papers." });

  function refresh() {
    empty.hidden = rows.length > 0;
  }
  function add(date = null, focus = false) {
    const row = dateRow(date, {
      onRemove: (el) => {
        rows.splice(rows.indexOf(row), 1);
        el.remove();
        refresh();
      },
    });
    rows.push(row);
    container.append(row.el);
    refresh();
    if (focus) row.focus();
    return row;
  }
  initial.forEach((d) => add(d));
  refresh();

  const addBtn = h("button", { type: "button", class: "btn btn-sm", onclick: () => add(null, true) }, icon("plus"), "Add a date");
  const el = h("div", {}, empty, container, h("div", { class: "mt-2" }, addBtn));

  function values() {
    const out = [];
    for (const row of rows) {
      const v = row.value();
      if (!v) return null;
      out.push(v);
    }
    return out;
  }
  return { el, values, add };
}
