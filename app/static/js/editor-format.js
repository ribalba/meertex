// LaTeX formatting in the browser editor (docs/DESIGN.md §10, The browser
// editor), like the bar over a comment box on GitHub. A bar over a LaTeX
// file's text has headings, bold, emphasis, monospace, inline math, lists,
// comments and an Insert menu. The common ones have keys as well.
//
// With text selected, a command wraps the selection. Without, it puts the
// command at the cursor with the cursor in its braces. The same command again
// takes the formatting away, so Ctrl+B in bold text removes its \textbf. Ctrl+I
// writes \emph and takes away \emph or \textit. Citations and references are
// tied to the word before with ~, as the papers here write them.
//
//   const format = editorFormat({ cm: () => cm, view: () => view, note });
//   format.el              the bar, over the text
//   format.extension()     the keys, in a LaTeX file's EditorState (editors only)
//   format.shown(on)       whether the open file gets the bar

import { h } from "./dom.js";
import { icon } from "./icons.js";
import { dropdown, openDialog } from "./ui.js";
import { environmentSnippet } from "./latexcomplete.js";

const MAC = /Mac|iPhone|iPad/.test(navigator.platform || "");

const HEADINGS = [["section", "Section"], ["subsection", "Subsection"], ["subsubsection", "Subsubsection"], ["paragraph", "Paragraph"]];
// A heading at the start of a line, after its indentation.
const HEADING_LINE = /^(\s*)\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)(\*?)\{/;
const LIST_BEGIN = /^\s*\\begin\{(itemize|enumerate|description)\}/;
const LIST_END = /^\s*\\end\{(itemize|enumerate|description)\}\s*$/;
// A command looks for the braces around the selection in its paragraph, at
// most this many lines up and down.
const PARAGRAPH_LINES = 200;

// What the shortcuts dialog lists. The formatting keys are this file's, the
// others come from CodeMirror, the page, the comments and the checker.
const SHORTCUTS = [
  ["Formatting", [
    ["Mod-b", "Bold, \\textbf"],
    ["Mod-i", "Emphasis, \\emph"],
    ["Mod-e", "Monospace, \\texttt"],
    ["Mod-Shift-m", "Inline math, $…$"],
    ["Mod-k", "Link, \\href"],
    ["Mod-/", "Comment the lines out, or in again"],
  ]],
  ["Editing", [
    ["Mod-z", "Undo"],
    ["Mod-Shift-z", "Redo"],
    ["Ctrl-Space", "Suggest commands, labels and keys"],
    ["Tab", "Take the suggestion, or indent"],
    ["Mod-]", "Indent the lines"],
    ["Mod-[", "Outdent the lines"],
    ["Alt-ArrowUp", "Move the line up"],
    ["Alt-ArrowDown", "Move the line down"],
    ["Shift-Alt-ArrowDown", "Copy the line down"],
    ["Mod-Shift-k", "Delete the line"],
  ]],
  ["Finding", [
    ["Mod-f", "Find and replace"],
    ["Mod-g", "Next match"],
    ["Mod-Alt-g", "Go to a line"],
    ["Alt-Shift-g", "Next spelling or grammar mistake"],
  ]],
  ["The paper", [
    ["Mod-Enter", "Compile"],
    ["Mod-Shift-Enter", "Show the cursor's place in the PDF"],
    ["Mod-Alt-m", "Comment on the selection"],
  ]],
];

export function editorFormat({ cm, view, note }) {
  let keys = null;

  // --- The commands. Each takes the view and returns true when it handled the key.

  const bold = wrapCommand("textbf");
  const emphasis = wrapCommand("emph", ["textit"]);
  const monospace = wrapCommand("texttt");

  // \name{…} around the selection, or taken away when the selection is in
  // one. `also` are commands the same key takes away, \textit for \emph.
  function wrapCommand(name, also = []) {
    const names = [name, ...also];
    return (v) => {
      const { state } = v;
      if (state.readOnly) return false;
      const range = state.selection.main;
      const group = groupsAround(state, range.from, range.to).find((g) => names.includes(g.name));
      if (group) return apply(v, [{ from: group.start, to: group.open + 1 }, { from: group.close, to: group.close + 1 }]);
      const { from, to, text } = trimmed(state, range);
      const whole = new RegExp(`^\\\\(?:${names.join("|")})\\*?\\{`).exec(text);
      if (whole && closing(text, whole[0].length - 1) === text.length - 1) {
        return apply(v, [{ from, to: from + whole[0].length }, { from: to - 1, to }], select(range, from, to - whole[0].length - 1));
      }
      if (!balanced(text)) {
        note("The selection has a brace without its partner. Select the whole group.");
        return true;
      }
      const open = `\\${name}{`;
      return apply(v, [{ from, insert: open }, { from: to, insert: "}" }],
        range.empty ? { anchor: from + open.length } : select(range, from + open.length, to + open.length));
    };
  }

  // $…$ around the selection, or taken away when the selection is in math.
  function inlineMath(v) {
    const { state } = v;
    if (state.readOnly) return false;
    const range = state.selection.main;
    const pair = dollarsAround(state, range.from, range.to);
    if (pair) return apply(v, [{ from: pair[0], to: pair[0] + 1 }, { from: pair[1], to: pair[1] + 1 }]);
    const { from, to, text } = trimmed(state, range);
    if (/^\$[^$]*[^\\$]\$$/.test(text)) return apply(v, [{ from, to: from + 1 }, { from: to - 1, to }], select(range, from, to - 2));
    return apply(v, [{ from, insert: "$" }, { from: to, insert: "$" }],
      range.empty ? { anchor: from + 1 } : select(range, from + 1, to + 1));
  }

  // The selected lines as `level` headings, or as plain text when `level` is
  // null. A heading of another level changes its level.
  function heading(level) {
    return (v) => {
      const { state } = v;
      if (state.readOnly) return false;
      const range = state.selection.main;
      const changes = [];
      for (const line of linesOf(state, range)) {
        const m = HEADING_LINE.exec(line.text);
        if (m) {
          const at = line.from + m[1].length;
          if (level) {
            if (m[2] !== level) changes.push({ from: at + 1, to: at + 1 + m[2].length, insert: level });
            continue;
          }
          const close = closing(line.text, m[0].length - 1);
          if (close >= 0) changes.push({ from: at, to: line.from + m[0].length }, { from: line.from + close, to: line.from + close + 1 });
        } else if (level) {
          const { indent, end } = content(line.text);
          if (end > indent && balanced(line.text.slice(indent, end))) {
            changes.push({ from: line.from + indent, insert: `\\${level}{` }, { from: line.from + end, insert: "}" });
          }
        }
      }
      if (changes.length) return apply(v, changes);
      if (level && range.empty && !state.doc.lineAt(range.head).text.trim()) {
        const open = `\\${level}{`;
        return apply(v, [{ from: range.head, insert: `${open}}` }], { anchor: range.head + open.length });
      }
      return true;
    };
  }

  // The selected lines as an itemize or enumerate list, an \item each. A
  // selected list of the other kind changes its kind. One of the same kind
  // goes back to plain lines.
  function list(env) {
    return (v) => {
      const { state } = v;
      if (state.readOnly) return false;
      const lines = linesOf(state, state.selection.main);
      const first = lines[0];
      const last = lines[lines.length - 1];
      const indent = /^\s*/.exec(first.text)[0];
      const unit = state.facet(cm().indentUnit);
      const begin = LIST_BEGIN.exec(first.text);
      const end = lines.length > 1 && LIST_END.exec(last.text);
      if (begin && end && begin[1] === end[1]) {
        if (begin[1] !== env) {
          const b = first.from + first.text.indexOf("{") + 1;
          const e = last.from + last.text.indexOf("{") + 1;
          return apply(v, [{ from: b, to: b + begin[1].length, insert: env }, { from: e, to: e + end[1].length, insert: env }]);
        }
        const plain = lines.slice(1, -1).map(({ text }) => (text.startsWith(indent + unit)
          ? indent + text.slice(indent.length + unit.length).replace(/^\\item(?![a-zA-Z]) ?/, "")
          : text)).join("\n");
        return apply(v, [{ from: first.from, to: last.to, insert: plain }], { anchor: first.from, head: first.from + plain.length });
      }
      if (lines.length === 1 && !first.text.trim()) {
        const head = `${indent}\\begin{${env}}\n${indent}${unit}\\item `;
        return apply(v, [{ from: first.from, to: first.to, insert: `${head}\n${indent}\\end{${env}}` }], { anchor: first.from + head.length });
      }
      // Lines indented deeper than the first, and comments, stay as they are.
      const items = lines.map(({ text }) => {
        if (!text.trim()) return "";
        const rest = text.startsWith(indent) ? text.slice(indent.length) : text.trimStart();
        return indent + unit + (/^(\s|%|\\item(?![a-zA-Z]))/.test(rest) ? rest : `\\item ${rest}`);
      }).join("\n");
      const insert = `${indent}\\begin{${env}}\n${items}\n${indent}\\end{${env}}`;
      return apply(v, [{ from: first.from, to: last.to, insert }], { anchor: first.from + insert.lastIndexOf("\n") });
    };
  }

  function comment(v) {
    return cm().toggleComment(v);
  }

  // \cite{} or \ref{} after the selection, tied to the word before it with ~,
  // with the list of keys or labels open.
  function tied(name) {
    return (v) => {
      const { state } = v;
      if (state.readOnly) return false;
      const { to } = trimmed(state, state.selection.main);
      const line = state.doc.lineAt(to);
      const before = state.sliceDoc(line.from, to);
      const word = before.trimEnd();
      const at = line.from + word.length;
      const tie = word && !/[~({[]$/.test(word) ? "~" : before.slice(word.length);
      const open = `${tie}\\${name}{`;
      apply(v, [{ from: at, to, insert: `${open}}` }], { anchor: at + open.length });
      setTimeout(() => cm().startCompletion(v), 0);
      return true;
    };
  }

  // \footnote{} right after the word before, without a space. The selection
  // becomes its text.
  function footnote(v) {
    const { state } = v;
    if (state.readOnly) return false;
    const range = state.selection.main;
    const { from, to, text } = trimmed(state, range);
    if (!balanced(text)) {
      note("The selection has a brace without its partner. Select the whole group.");
      return true;
    }
    const line = state.doc.lineAt(from);
    const word = state.sliceDoc(line.from, from).trimEnd();
    const at = word ? line.from + word.length : from;
    const open = "\\footnote{";
    return apply(v, [{ from: at, to, insert: `${open}${text}}` }],
      range.empty ? { anchor: at + open.length } : select(range, at + open.length, at + open.length + text.length));
  }

  // \href{}{…} around the selection with the cursor at the address, \url{…}
  // around a selected address, or \href with fields to fill.
  function link(v) {
    const { state } = v;
    if (state.readOnly) return false;
    const range = state.selection.main;
    const { from, to, text } = trimmed(state, range);
    if (range.empty || !text.trim()) {
      cm().snippet("\\href{${url}}{${text}}")(v, null, range.head, range.head);
      return true;
    }
    if (/^(https?:\/\/|www\.)\S+$/.test(text)) return apply(v, [{ from, insert: "\\url{" }, { from: to, insert: "}" }]);
    if (!balanced(text)) {
      note("The selection has a brace without its partner. Select the whole group.");
      return true;
    }
    return apply(v, [{ from, insert: "\\href{}{" }, { from: to, insert: "}" }], { anchor: from + "\\href{".length });
  }

  // A figure, table or equation on a line of its own under the cursor's line,
  // or in its place when that line is empty. Tab moves through its fields.
  function environment(name) {
    return (v) => {
      const { state } = v;
      if (state.readOnly) return false;
      const line = state.doc.lineAt(state.selection.main.head);
      const blank = !line.text.trim();
      const at = line.to;
      // The snippet replaces the selection when it starts at `at`.
      v.dispatch({ selection: { anchor: at } });
      cm().snippet(blank ? environmentSnippet(name) : `\n${environmentSnippet(name)}`)(v, null, blank ? line.from + /^\s*/.exec(line.text)[0].length : at, at);
      return true;
    };
  }

  // --- The bar ---------------------------------------------------------------------

  // Run `command` on the open text and give the text the focus back.
  function run(command) {
    const v = view();
    if (!v) return;
    command(v);
    v.focus();
  }

  function button(iconName, label, command, key) {
    const b = h("button", {
      type: "button", class: "btn-octicon", "aria-label": label, title: key ? `${label} (${keyLabel(key)})` : label,
    }, icon(iconName));
    // The text keeps the focus, so its selection stays visible.
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", () => run(command));
    return b;
  }

  function menu(iconName, label, items, align = "left") {
    const d = dropdown({ summary: [icon(iconName), icon("triangle-down")], summaryClass: "btn-octicon editor-format-menu", label, items, align });
    d.querySelector("summary").title = label;
    return d;
  }

  const code = (text) => h("code", { class: "color-muted", text });
  const headingAt = () => {
    const v = view();
    return v ? HEADING_LINE.exec(v.state.doc.lineAt(v.state.selection.main.head).text)?.[2] ?? null : null;
  };
  const separator = () => h("span", { class: "editor-format-sep", "aria-hidden": "true" });

  const el = h("div", { class: "editor-format", role: "toolbar", "aria-label": "Formatting", hidden: true },
    menu("heading", "Heading", () => {
      const now = headingAt();
      return [
        { text: "Plain text", checked: !now, onClick: () => run(heading(null)) },
        ...HEADINGS.map(([name, text]) => ({ text, checked: now === name, trailing: code(`\\${name}`), onClick: () => run(heading(name)) })),
      ];
    }),
    separator(),
    button("bold", "Bold", bold, "Mod-b"),
    button("italic", "Emphasis", emphasis, "Mod-i"),
    button("code", "Monospace", monospace, "Mod-e"),
    button("sigma", "Inline math", inlineMath, "Mod-Shift-m"),
    separator(),
    button("list-unordered", "Bulleted list", list("itemize")),
    button("list-ordered", "Numbered list", list("enumerate")),
    separator(),
    button("percent", "Comment the lines out, or in again", comment, "Mod-/"),
    menu("plus", "Insert", [
      { text: "Citation", trailing: code("~\\cite{}"), onClick: () => run(tied("cite")) },
      { text: "Reference", trailing: code("~\\ref{}"), onClick: () => run(tied("ref")) },
      { text: "Footnote", trailing: code("\\footnote{}"), onClick: () => run(footnote) },
      { text: "Link", trailing: code("\\href{}{}"), onClick: () => run(link) },
      "divider",
      { text: "Figure", trailing: code("figure"), onClick: () => run(environment("figure")) },
      { text: "Table", trailing: code("table"), onClick: () => run(environment("table")) },
      { text: "Equation", trailing: code("equation"), onClick: () => run(environment("equation")) },
    // Opening to the left, it stays on a phone's screen.
    ], "right"),
    h("button", {
      type: "button", class: "btn-octicon editor-format-keys hide-sm", "aria-label": "Keyboard shortcuts", title: "Keyboard shortcuts",
      onclick: showShortcuts,
    }, icon("keyboard")));

  // Left and right move between the bar's buttons.
  el.addEventListener("keydown", (e) => {
    if ((e.key !== "ArrowLeft" && e.key !== "ArrowRight") || e.target.closest(".dropdown-menu")) return;
    const all = [...el.querySelectorAll(":scope > button, :scope > details > summary")].filter((b) => b.offsetParent);
    const i = all.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    all[(i + (e.key === "ArrowRight" ? 1 : -1) + all.length) % all.length].focus();
  });

  function showShortcuts() {
    const kbd = (spec) => keyParts(spec).map((part) => h("kbd", { text: part }));
    openDialog({
      title: "Keyboard shortcuts",
      wide: true,
      body: h("div", { class: "shortcuts" }, SHORTCUTS.map(([title, rows]) => h("section", {},
        h("h3", { text: title }),
        h("dl", {}, rows.map(([spec, what]) => [h("dt", {}, kbd(spec)), h("dd", { text: what })]))))),
      onClose: () => view()?.focus(),
    });
  }

  return {
    el,
    extension() {
      keys ??= cm().keymap.of([
        { key: "Mod-b", run: bold },
        { key: "Mod-i", run: emphasis },
        { key: "Mod-e", run: monospace },
        { key: "Mod-Shift-m", run: inlineMath },
        { key: "Mod-k", run: link },
      ]);
      return keys;
    },
    shown(on) {
      el.hidden = !on;
    },
  };
}

// --- Reading the text ----------------------------------------------------------------

// Apply `changes` in one step. The selection moves with the text, or goes to `selection`.
function apply(v, changes, selection) {
  const set = v.state.changes(changes);
  v.dispatch({ changes: set, selection: selection ?? v.state.selection.map(set), scrollIntoView: true, userEvent: "input.format" });
  return true;
}

// from..to as a selection that keeps the direction of `range`.
function select(range, from, to) {
  return range.head < range.anchor ? { anchor: to, head: from } : { anchor: from, head: to };
}

// The selection without the spaces at its ends, which belong outside the braces.
function trimmed(state, range) {
  const text = state.sliceDoc(range.from, range.to);
  if (!text.trim()) return { from: range.from, to: range.to, text };
  const from = range.from + text.length - text.trimStart().length;
  const to = range.to - (text.length - text.trimEnd().length);
  return { from, to, text: state.sliceDoc(from, to) };
}

// The lines the selection touches. One that ends at the start of a line leaves that line out.
function linesOf(state, range) {
  const doc = state.doc;
  const first = doc.lineAt(range.from).number;
  let last = doc.lineAt(range.to).number;
  if (last > first && doc.line(last).from === range.to) last -= 1;
  const lines = [];
  for (let n = first; n <= last; n++) lines.push(doc.line(n));
  return lines;
}

// Where a line's text starts after its indentation, and where it ends before
// a comment and the spaces in front of it.
function content(text) {
  const indent = text.length - text.trimStart().length;
  let end = text.length;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\\") i++;
    else if (text[i] === "%") {
      end = i;
      break;
    }
  }
  while (end > indent && /\s/.test(text[end - 1])) end -= 1;
  return { indent, end };
}

// The lines around from..to up to a blank line, where a group must close.
function paragraph(doc, from, to) {
  let first = doc.lineAt(from);
  let last = doc.lineAt(to);
  for (let n = 0; n < PARAGRAPH_LINES && first.number > 1; n++) {
    const prev = doc.line(first.number - 1);
    if (!prev.text.trim()) break;
    first = prev;
  }
  for (let n = 0; n < PARAGRAPH_LINES && last.number < doc.lines; n++) {
    const next = doc.line(last.number + 1);
    if (!next.text.trim()) break;
    last = next;
  }
  return { start: first.from, text: doc.sliceString(first.from, last.to) };
}

// Calls `fn` with each brace and dollar of `text` that is not escaped or in a comment.
function eachMark(text, fn) {
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === "\\") i++;
    else if (c === "%") {
      const end = text.indexOf("\n", i);
      if (end < 0) return;
      i = end;
    } else if (c === "{" || c === "}" || c === "$") fn(c, i);
  }
}

// The brace groups that hold from..to, innermost first. Each has the command
// written right before its brace (`name`, or null for a bare group or a second
// argument), where that command starts, and the positions of its braces.
function groupsAround(state, from, to) {
  const { start, text } = paragraph(state.doc, from, to);
  const open = [];
  const found = [];
  eachMark(text, (c, i) => {
    if (c === "{") open.push(i);
    else if (c === "}" && open.length) {
      const at = open.pop();
      if (start + at >= from || start + i < to) return;
      const m = /\\([a-zA-Z]+)\*?$/.exec(text.slice(Math.max(0, at - 40), at));
      found.push({ name: m ? m[1] : null, start: start + at - (m ? m[0].length : 0), open: start + at, close: start + i });
    }
  });
  return found;
}

// The two dollars of the inline math that holds from..to, or null.
function dollarsAround(state, from, to) {
  const { start, text } = paragraph(state.doc, from, to);
  let open = -1;
  let found = null;
  eachMark(text, (c, i) => {
    if (c !== "$" || found) return;
    if (open < 0) {
      open = i;
      return;
    }
    if (start + open < from && start + i >= to) found = [start + open, start + i];
    open = -1;
  });
  return found;
}

// The position of the brace that closes the one at `open`, or -1.
function closing(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    const c = text[i];
    if (c === "\\") i++;
    else if (c === "{") depth += 1;
    else if (c === "}" && --depth === 0) return i;
  }
  return -1;
}

function balanced(text) {
  let depth = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === "\\") i++;
    else if (c === "{") depth += 1;
    else if (c === "}" && --depth < 0) return false;
  }
  return depth === 0;
}

// --- Keys --------------------------------------------------------------------------

const KEY_NAMES = { ArrowUp: "↑", ArrowDown: "↓", Space: "Space" };

// A CodeMirror key like "Mod-Shift-m" as the keys to press on this computer.
function keyParts(spec) {
  const parts = spec.split("-");
  const key = parts.pop();
  const names = MAC ? { Mod: "⌘", Shift: "⇧", Alt: "⌥", Ctrl: "⌃" } : { Mod: "Ctrl", Shift: "Shift", Alt: "Alt", Ctrl: "Ctrl" };
  return [...parts.map((p) => names[p] || p), KEY_NAMES[key] || (key.length === 1 ? key.toUpperCase() : key)];
}

function keyLabel(spec) {
  return keyParts(spec).join(MAC ? "" : "+");
}
