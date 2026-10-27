// Renders the diff route's output the way GitHub does. Each file gets a box
// with old and new line numbers and green and red lines. A word diff marks the
// changed words inline with <ins> and <del>.

import { h } from "./dom.js";
import { icon } from "./icons.js";
import { plural } from "./dom.js";

const STATUS = {
  added: ["Added", "label-success"],
  deleted: ["Deleted", "label-danger"],
  modified: ["Modified", "label-secondary"],
  renamed: ["Renamed", "label-accent"],
};

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/;
const BIG_DIFF_LINES = 1500;

function row(cls, oldNo, newNo, content) {
  return h("tr", { class: cls },
    h("td", { class: "num", dataset: { n: oldNo ?? "" } }),
    h("td", { class: "num", dataset: { n: newNo ?? "" } }),
    h("td", { class: "code" }, content),
  );
}

// Unified diff text to table rows. Header lines before the first hunk are skipped.
export function unifiedRows(text) {
  const rows = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;
  let adds = 0;
  let dels = 0;
  const lines = String(text || "").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  for (const line of lines) {
    const m = HUNK.exec(line);
    if (m) {
      inHunk = true;
      oldNo = Number(m[1]);
      newNo = Number(m[2]);
      rows.push(row("hunk", "", "", line));
      continue;
    }
    if (!inHunk) continue;
    const c = line[0];
    const rest = line.slice(1);
    if (c === "+") { rows.push(row("add", "", newNo++, rest)); adds += 1; }
    else if (c === "-") { rows.push(row("del", oldNo++, "", rest)); dels += 1; }
    else if (c === "\\") rows.push(row("note", "", "", rest.trim()));
    else rows.push(row("ctx", oldNo++, newNo++, c === " " ? rest : line));
  }
  return { rows, adds, dels };
}

// git's --word-diff=porcelain has one token per line. A leading " " is common
// text, "-" removed, "+" added, and "~" ends a line of the file.
export function wordRows(text) {
  const rows = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;
  let segs = [];
  let adds = 0;
  let dels = 0;
  const flush = () => {
    const hasOld = segs.length === 0 || segs.some((s) => s.t !== "+");
    const hasNew = segs.length === 0 || segs.some((s) => s.t !== "-");
    const changed = segs.some((s) => s.t !== " ");
    const onlyAdd = segs.length > 0 && segs.every((s) => s.t === "+");
    const onlyDel = segs.length > 0 && segs.every((s) => s.t === "-");
    const content = segs.map((s) => (s.t === "+" ? h("ins", { text: s.s }) : s.t === "-" ? h("del", { text: s.s }) : s.s));
    const cls = onlyAdd ? "add" : onlyDel ? "del" : changed ? "mixed" : "ctx";
    rows.push(row(cls, hasOld ? oldNo++ : "", hasNew ? newNo++ : "", content));
    segs = [];
  };
  const lines = String(text || "").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  for (const line of lines) {
    const m = HUNK.exec(line);
    if (m) {
      if (segs.length) flush();
      inHunk = true;
      oldNo = Number(m[1]);
      newNo = Number(m[2]);
      rows.push(row("hunk", "", "", line));
      continue;
    }
    if (!inHunk) continue;
    const c = line[0];
    if (c === "~") { flush(); continue; }
    if (c === "+") adds += 1;
    if (c === "-") dels += 1;
    if (c === " " || c === "+" || c === "-") segs.push({ t: c, s: line.slice(1) });
  }
  if (segs.length) flush();
  return { rows, adds, dels };
}

function fileBox(file, word) {
  const [statusText, statusCls] = STATUS[file.status] || [file.status || "Changed", "label-secondary"];
  const parsed = file.binary ? null : (word ? wordRows : unifiedRows)(file.diff);
  const total = parsed ? parsed.rows.length : 0;
  const toggle = h("button", { type: "button", class: "btn-octicon", "aria-expanded": "true", "aria-label": "Collapse file" }, icon("chevron-down"));
  const header = h("div", { class: "box-header" },
    toggle,
    h("span", { class: `Label ${statusCls}`, text: statusText }),
    parsed && !word ? h("span", { class: "text-small nowrap" },
      h("span", { class: "diff-stat-add", text: `+${parsed.adds}` }), " ",
      h("span", { class: "diff-stat-del", text: `-${parsed.dels}` })) : null,
    h("span", { class: "path flex-1" },
      file.status === "renamed" && file.old_path ? `${file.old_path} → ${file.path}` : file.path),
  );
  const body = h("div", { class: "diff-body" });
  const box = h("div", { class: "box diff-file", id: `diff-${encodeURIComponent(file.path)}` }, header, body);

  const fill = () => {
    if (file.binary) body.append(h("div", { class: "diff-empty", text: "Binary file not shown." }));
    else if (!total) body.append(h("div", { class: "diff-empty", text: file.status === "renamed" ? "File renamed without changes." : "No changes to show." }));
    else body.append(h("table", { class: "diff-table" }, h("tbody", {}, parsed.rows)));
  };
  // Very long diffs wait for a click, so a page with a big .bib stays quick.
  if (total > BIG_DIFF_LINES) {
    const load = h("button", { type: "button", class: "btn-link", text: "Load diff" });
    load.addEventListener("click", () => { body.replaceChildren(); fill(); });
    body.append(h("div", { class: "diff-empty" }, `Large diff, ${plural(total, "line")}. `, load));
  } else {
    fill();
  }
  toggle.addEventListener("click", () => {
    const collapsed = box.classList.toggle("collapsed");
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Expand file" : "Collapse file");
    toggle.replaceChildren(icon(collapsed ? "chevron-right" : "chevron-down"));
  });
  return { box, adds: parsed?.adds || 0, dels: parsed?.dels || 0 };
}

export function renderDiff(diff, { word = false } = {}) {
  const files = diff?.files || [];
  if (!files.length) {
    return h("div", { class: "box" }, h("div", { class: "blankslate" },
      icon("check-circle", { size: 24 }),
      h("h3", { text: "Nothing changed" }),
      h("p", { text: "The two states have the same files." })));
  }
  const boxes = files.map((f) => fileBox(f, word));
  const adds = boxes.reduce((n, b) => n + b.adds, 0);
  const dels = boxes.reduce((n, b) => n + b.dels, 0);
  const summary = h("div", { class: "diff-summary" },
    h("span", {}, "Showing ", h("strong", { text: plural(files.length, "changed file") }),
      word ? " as a word diff." : [" with ", h("span", { class: "diff-stat-add", text: plural(adds, "addition") }),
        " and ", h("span", { class: "diff-stat-del", text: plural(dels, "deletion") }), "."]),
  );
  return h("div", {}, summary, boxes.map((b) => b.box));
}
