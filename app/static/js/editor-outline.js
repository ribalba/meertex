// The outline under the editor's file tree (docs/DESIGN.md §10, The browser
// editor). The document's headings, \part to \subparagraph, as a tree. It
// starts at the main file and follows \input, \include, \subfile and \import
// in the order LaTeX reads them. A .tex file the main file does not read shows
// its own headings. A click opens a heading's line, and the heading the cursor
// is under is marked. The box folds to its header, remembered per browser,
// and a folded box does no work.
//
// The headings come from the completions' index (latexcomplete.js), which
// reads a file again only after it changed.
//
//   const outline = editorOutline({ cm: () => cm, read: () => index.read(byPath),
//     main: () => mainFile, place: () => cursor, open: (at) => openAt(at), href: (path, line) => url });
//   outline.el                         under the file list
//   extensions: outline.extension()    in every text file's EditorState
//   outline.changed()                  a file's text or the list of files changed
//   outline.moved()                    another file was opened
//   outline.destroy()                  leaving the page

import { h, clear } from "./dom.js";
import { icon } from "./icons.js";

const KEY = "meertex.editor.outline";
const TEX = /\.tex$/i;
// While someone types, the outline follows at most this often.
const DELAY_MS = 300;

function parentOf(path) {
  const i = path.lastIndexOf("/");
  return i >= 0 ? path.slice(0, i) : "";
}

// `name` seen from `folder`, without . and ..
function joinPath(folder, name) {
  const parts = [];
  for (const part of `${folder}/${name}`.split("/")) {
    if (part === "..") parts.pop();
    else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/");
}

// The file an \input names. LaTeX runs in the main file's folder and tries
// the name with .tex first. Names seen from the reading file or from the
// paper's top folder are found too.
function resolve(name, folders, outlines) {
  for (const folder of folders) {
    const path = joinPath(folder, name);
    if (outlines.has(`${path}.tex`)) return `${path}.tex`;
    if (outlines.has(path)) return path;
  }
  return null;
}

// The headings of the document that starts at `root`, in reading order.
// `starts` has the number of headings read before each file, and `marks` each
// file's lines with the number read up to and with that line.
function walk(root, outlines) {
  const doc = { root, items: [], starts: new Map(), marks: new Map() };
  const top = parentOf(root);
  const read = (path) => {
    if (doc.starts.has(path)) return;
    doc.starts.set(path, doc.items.length);
    const marks = [];
    doc.marks.set(path, marks);
    for (const item of outlines.get(path)) {
      if (item.input === undefined) doc.items.push({ ...item, path });
      else {
        const target = resolve(item.input, [top, parentOf(path), ""], outlines);
        if (target) read(target);
      }
      marks.push({ line: item.line, upto: doc.items.length });
    }
  };
  if (outlines.has(root)) read(root);
  return doc;
}

// Each heading goes under the last one before it with a smaller level.
function nest(items) {
  const nodes = [];
  const roots = [];
  const stack = [];
  items.forEach((item, index) => {
    while (stack.length && stack[stack.length - 1].level >= item.level) stack.pop();
    const parent = stack[stack.length - 1];
    const node = { ...item, index, parent: parent ? parent.index : -1, children: [], key: `${item.path}\n${item.level}\n${item.title}` };
    (parent ? parent.children : roots).push(node);
    stack.push(node);
    nodes.push(node);
  });
  return { roots, nodes };
}

function readShown() {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

export function editorOutline({ cm, read, main, place, open, href }) {
  let shown = readShown();
  let stale = true;
  let dead = false;
  let doc = null;
  let tree = { roots: [], nodes: [] };
  let rows = new Map(); // heading index -> its row, for the headings not folded away
  let signature = null;
  let current = null;
  let builtFor = null; // the cursor's file when the outline was built
  let timer = null;
  let frame = 0;
  let listener = null;
  const folded = new Set();

  const body = h("div", { class: "editor-outline-body", id: "editor-outline" });
  const toggle = h("button", { type: "button", class: "editor-outline-toggle", "aria-controls": body.id });
  const el = h("section", { class: "editor-outline", "aria-label": "Outline" },
    h("h2", { class: "editor-outline-head" }, toggle), body);

  toggle.addEventListener("click", () => {
    shown = !shown;
    try {
      localStorage.setItem(KEY, shown ? "on" : "off");
    } catch {
      // Private windows may refuse. The choice holds for this page.
    }
    paintFold();
    if (shown && stale) build();
  });
  body.addEventListener("click", (e) => {
    const link = e.target.closest("a.tree-link[data-index]");
    if (!link || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    const node = tree.nodes[Number(link.dataset.index)];
    if (node) open({ path: node.path, line: node.line });
  });
  paintFold();

  function paintFold() {
    el.classList.toggle("is-open", shown);
    body.hidden = !shown;
    toggle.setAttribute("aria-expanded", String(shown));
    clear(toggle).append(icon(shown ? "chevron-down" : "chevron-right", { size: 12 }), h("span", { text: "Outline" }));
  }

  function changed() {
    stale = true;
    if (dead || !shown || timer) return;
    timer = setTimeout(() => {
      timer = null;
      build();
    }, DELAY_MS);
  }

  function moved() {
    if (dead || !shown || frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if ((place()?.path ?? null) !== builtFor) build();
      else mark();
    });
  }

  function build() {
    const index = read();
    if (dead || !index) return;
    stale = false;
    const at = place();
    builtFor = at?.path ?? null;
    doc = walk(main(), index.outlines);
    if (at && TEX.test(at.path) && index.outlines.has(at.path) && !doc.starts.has(at.path)) doc = walk(at.path, index.outlines);
    tree = nest(doc.items);
    paint();
    mark();
  }

  function paint() {
    const next = JSON.stringify([doc.root, doc.starts.size, doc.items.map((i) => [i.level, i.title, i.path, i.line]), [...folded]]);
    if (next === signature) return;
    signature = next;
    rows = new Map();
    current = null;
    if (!doc.items.length) {
      clear(body).append(h("p", { class: "editor-tree-empty", text: doc.starts.size ? "No headings yet." : `There is no ${doc.root} yet.` }));
      return;
    }
    const list = h("ul", { class: "tree" });
    const add = (node, depth) => {
      const name = node.title || "Untitled";
      const shut = node.children.length > 0 && folded.has(node.key);
      const row = h("div", { class: "tree-row", style: { paddingLeft: `${depth * 12}px` } },
        node.children.length ? h("button", {
          type: "button", class: "tree-toggle", "aria-label": `${shut ? "Expand" : "Collapse"} ${name}`, "aria-expanded": String(!shut),
          onclick: () => flip(node.key),
        }, icon(shut ? "chevron-right" : "chevron-down", { size: 12 })) : h("span", { class: "tree-toggle spacer" }),
        h("a", { class: "tree-link", href: href(node.path, node.line), title: `${name} · ${node.path}:${node.line}`, dataset: { index: String(node.index) } },
          h("span", { class: ["flex-1 truncate", node.title ? null : "untitled"], text: name })));
      rows.set(node.index, row);
      list.append(h("li", {}, row));
      if (!shut) node.children.forEach((child) => add(child, depth + 1));
    };
    tree.roots.forEach((node) => add(node, 0));
    const scroll = body.scrollTop;
    clear(body).append(list);
    body.scrollTop = scroll;
  }

  function flip(key) {
    if (!folded.delete(key)) folded.add(key);
    paint();
    mark();
  }

  // The heading the cursor is under is the last one LaTeX reads before the
  // cursor's line. Inside a folded heading, the folded one is marked.
  function mark() {
    const at = place();
    const marks = at && doc?.marks.get(at.path);
    let index = -1;
    if (marks) {
      let upto = doc.starts.get(at.path);
      for (const m of marks) {
        if (m.line > at.line) break;
        upto = m.upto;
      }
      index = upto - 1;
    }
    while (index >= 0 && !rows.has(index)) index = tree.nodes[index].parent;
    const row = rows.get(index) || null;
    if (row === current) return;
    current?.removeAttribute("aria-current");
    current = row;
    if (!row) return;
    row.setAttribute("aria-current", "location");
    // Only the outline scrolls to keep the row in sight.
    const r = row.getBoundingClientRect();
    const b = body.getBoundingClientRect();
    if (r.top < b.top) body.scrollTop -= b.top - r.top;
    else if (r.bottom > b.bottom) body.scrollTop += r.bottom - b.bottom;
  }

  function extension() {
    listener ??= cm().EditorView.updateListener.of((u) => {
      if (u.selectionSet || u.docChanged) moved();
    });
    return listener;
  }

  function destroy() {
    dead = true;
    clearTimeout(timer);
    cancelAnimationFrame(frame);
  }

  return { el, extension, changed, moved, destroy };
}
