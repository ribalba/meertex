// /p/<name>/edit[/<path>] shows the paper's live document in the browser, edited
// together with everybody who has it open, in a browser or in a synced folder.
//
// The page holds a full copy of the document (yjs) and keeps it in step over
// the y-websocket endpoint (docs/DESIGN.md §10). Typing goes straight into the
// document. New files, renames, deletes and uploads go through the HTTP API,
// which checks the paths, and come back over the socket like anyone's edit.
// The page follows files by id, so a rename by a coauthor keeps the file open.
//
// CodeMirror, yjs and y-websocket are one vendored module (tools/editor),
// loaded only on this page.

import { api, encPath, projectUrl } from "../api.js";
import { h, append, clear, formatBytes, plural } from "../dom.js";
import { icon } from "../icons.js";
import { decodePath, onLeave, onPop } from "../router.js";
import { session } from "../session.js";
import { avatar, blankslate, confirmDialog, dropdown, field, flash, flashError, openDialog, setTitle, spinner } from "../ui.js";
import { FILES, materialize, pathProblem } from "../livefiles.js";
import { ProjectIndex, latexAutocomplete } from "../latexcomplete.js";
import { pdfPane } from "./editor-pdf.js";
import { editorComments } from "../editor-comments.js";
import { editorGrammar } from "../editor-grammar.js";
import { editorOutline } from "../editor-outline.js";
import { editorProblems } from "../editor-problems.js";
import { showSpot, spotExtension } from "../editor-spot.js";

const BUNDLE = "/static/vendor/editor/editor.js";
// y-websocket drops a socket that received nothing for 30 seconds, and a
// writer alone on a paper receives nothing. Asking for the server's state
// every 15 seconds gets a small answer back, which keeps the socket open.
const RESYNC_MS = 15000;
// Close codes after which reconnecting cannot help (routers/sync.py).
const CLOSED = {
  4001: "Your session has ended. Sign in again to keep editing.",
  4004: "This paper is no longer shared with you.",
};
// The server closes with this when someone changed your role. The socket comes
// back by itself with the new rights, but the page decided read-only or not
// when it loaded.
const ROLE_CHANGED = 4009;
const SPELLCHECK_KEY = "meertex.editor.spellcheck";
const PDF_KEY = "meertex.editor.pdf";
const PDF_WIDTH_KEY = "meertex.editor.pdfwidth";
const TREE_WIDTH_KEY = "meertex.editor.treewidth";
// The folders open in the tree, per paper: FOLDERS_KEY.<project id>.
const FOLDERS_KEY = "meertex.editor.folders";
const PHONE = "(max-width: 767px)";
const IMAGE = /\.(png|jpe?g|gif|webp)$/i;
const LATEX = /\.(tex|sty|cls|ltx|dtx|ins|bbx|cbx|lbx|tikz)$/i;
const DRAG_PATH = "application/x-meertex-path";
// The colours and the hash of ui.js's avatar(), so a cursor has the colour of
// its person's avatar.
const COLORS = ["#8a6a3b", "#2f6f9f", "#6b7d2a", "#8250df", "#bf5a2a", "#1a7f73", "#b35900", "#4b5fa8", "#9a3c6e", "#3c7d4f"];

let bundle = null;
let looks = null;

function loadBundle() {
  bundle ??= import(BUNDLE).catch((err) => {
    bundle = null;
    throw err;
  });
  return bundle;
}

export function editorHref(base, path = "") {
  return `${base}/edit${path ? `/${encPath(path)}` : ""}`;
}

function colorFor(email) {
  let hash = 0;
  for (const c of email || "?") hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  return COLORS[hash % COLORS.length];
}

function parentOf(path) {
  const i = path.lastIndexOf("/");
  return i >= 0 ? path.slice(0, i) : "";
}

function joinPath(folder, name) {
  return folder ? `${folder}/${name}` : name;
}

function stored(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private windows may refuse. The choice holds for this page.
  }
}

function readSpellcheck() {
  try {
    return localStorage.getItem(SPELLCHECK_KEY) === "on";
  } catch {
    return false;
  }
}

// The folders left open the last time. Every other folder starts closed.
function readFolders(projectId) {
  try {
    const list = JSON.parse(localStorage.getItem(`${FOLDERS_KEY}.${projectId}`) || "[]");
    return new Set(Array.isArray(list) ? list.filter((p) => typeof p === "string") : []);
  } catch {
    return new Set();
  }
}

function saveFolders(ed) {
  store(`${FOLDERS_KEY}.${ed.p.id}`, JSON.stringify([...ed.expanded]));
}

// Open `folder` and the folders above it, for a file made or uploaded there.
function expand(ed, folder) {
  for (let p = folder; p; p = parentOf(p)) ed.expanded.add(p);
  saveFolders(ed);
  paintTree(ed);
}

function byName(a, b) {
  return a.name.localeCompare(b.name, undefined, { numeric: true });
}

// --- The page ----------------------------------------------------------------

export async function render(ctx) {
  const want = decodePath(ctx.sub.slice(1).join("/"));
  setTitle(want || "Editor", ctx.project.name);
  const ed = layout(ctx);
  clear(ctx.main).append(ed.el.shell);
  ed.pdf = pdfPane({
    project: ctx.project,
    openSource: (at) => openAt(ed, at),
    cursor: () => cursorPlace(ed),
    note: (text) => note(ed, text),
    onHide: () => togglePdf(ed, false),
    onProblems: (problems) => ed.problems.update(problems),
    hasFile: (path) => (ed.ready ? ed.byPath.has(path) : null),
    canEdit: ed.canEdit,
  });
  ed.el.pdfColumn.append(ed.pdf.el);
  onLeave(() => ed.pdf.destroy());
  onLeave(() => ed.comments.destroy());
  onLeave(() => ed.outline.destroy());
  ed.grammar.start();
  onLeave(() => ed.grammar.destroy());
  // The page goes up right away and connects in the background.
  start(ed, want).catch((err) => {
    if (!ctx.alive()) return;
    console.error(err);
    clear(ed.el.treeBody);
    showMessage(ed, blankslate({
      iconName: "alert",
      title: "The editor could not start",
      text: "Check your connection and reload the page.",
      action: h("button", { type: "button", class: "btn", text: "Reload", onclick: () => location.reload() }),
    }));
  });
}

function layout(ctx) {
  const ed = {
    ctx,
    p: ctx.project,
    base: ctx.base,
    canEdit: ctx.canEdit,
    spell: readSpellcheck(),
    byPath: new Map(),
    byId: new Map(),
    expanded: readFolders(ctx.project.id),
    undo: new Map(),
    selections: new Map(),
    openId: null,
    openPath: null,
    offs: [],
    el: {},
  };
  const el = ed.el;

  el.upload = h("input", { type: "file", multiple: true, hidden: true });
  el.upload.addEventListener("change", () => {
    const files = [...el.upload.files];
    el.upload.value = "";
    upload(ed, files, el.upload.dataset.folder || "");
  });
  const addMenu = ed.canEdit ? dropdown({
    summary: [icon("plus"), icon("triangle-down")],
    summaryClass: "btn btn-sm",
    label: "Add a file",
    align: "right",
    items: [
      { text: "New file…", icon: "file", onClick: () => newFile(ed, "") },
      { text: "New folder…", icon: "folder", onClick: () => newFolder(ed, "") },
      { text: "Upload files…", icon: "upload", onClick: () => pickUpload(ed, "") },
    ],
  }) : null;

  el.treeBody = h("div", { class: "editor-tree-body" }, h("div", { class: "editor-tree-loading" }, spinner(), " Loading files…"));
  // The document's headings under the files (editor-outline.js).
  ed.outline = editorOutline({
    cm: () => ed.cm,
    read: () => (ed.ready ? ed.index.read(ed.byPath) : null),
    main: () => ed.p.main_file || "main.tex",
    place: () => cursorPlace(ed),
    open: (at) => openAt(ed, at),
    href: (path, line) => `${editorHref(ed.base, path)}#L${line}`,
  });
  el.tree = h("nav", { class: "editor-tree", id: `tree-${ed.p.id}`, "aria-label": "Files of this paper" },
    h("div", { class: "editor-tree-head" }, h("h2", { text: "Files" }), addMenu, el.upload,
      h("button", { type: "button", class: "btn-octicon editor-tree-close", "aria-label": "Back to the editor", onclick: () => showTree(ed, false) }, icon("x"))),
    el.treeBody, ed.outline.el);
  wireTree(ed);

  el.filesBtn = h("button", {
    type: "button", class: "btn btn-sm editor-files-btn", "aria-controls": el.tree.id, "aria-expanded": "false",
  }, icon("folder"), "Files");
  el.filesBtn.addEventListener("click", () => showTree(ed, !el.shell.classList.contains("show-tree")));
  el.path = h("div", { class: "editor-path" });
  el.note = h("span", { class: "editor-note", role: "status" });
  el.statusText = h("span", { class: "editor-status-text", text: "Connecting…" });
  el.status = h("span", { class: "editor-status is-pending", role: "status" }, h("span", { class: "dot" }), el.statusText);
  el.presence = h("div", { class: "editor-presence", hidden: true });
  // With a checker on the server, the count of underlines in the open file.
  el.spellCount = h("span", { class: "counter", hidden: true });
  el.spell = h("button", {
    type: "button", class: "btn btn-sm editor-spell-btn", "aria-pressed": String(ed.spell), "aria-label": "Spellcheck",
    title: "Underline misspelled words with the browser's spellchecker",
  }, icon("check"), h("span", { class: "hide-sm hide-narrow", text: "Spellcheck" }), el.spellCount);
  el.spell.addEventListener("click", () => toggleSpellcheck(ed));
  el.pdfBtn = h("button", { type: "button", class: "btn btn-sm", "aria-label": "PDF", title: "Show or hide the PDF" },
    icon("pdf"), h("span", { class: "hide-sm hide-narrow", text: "PDF" }));
  el.pdfBtn.addEventListener("click", () => togglePdf(ed));
  // Comments on the text (editor-comments.js). Their panel takes the PDF's column.
  ed.comments = editorComments({
    project: ed.p,
    base: ed.base,
    cm: () => ed.cm,
    view: () => (ed.boundText ? ed.view : null),
    path: () => (ed.boundText ? ed.openPath : null),
    reveal: (from) => reveal(ed, from),
    openFile: (path, line) => openAt(ed, { path, line }),
    show: (on) => showComments(ed, on),
    note: (text) => note(ed, text),
  });
  // The last compile's errors and warnings on their lines (editor-problems.js).
  ed.problems = editorProblems({
    cm: () => ed.cm,
    doc: () => ed.doc,
    files: () => ed.byPath,
    view: () => (ed.boundText ? ed.view : null),
    text: () => ed.boundText,
  });
  // Spelling and grammar from the server's LanguageTool (editor-grammar.js).
  // Without one the Spellcheck button stays the browser's spellchecker.
  ed.grammar = editorGrammar({
    cm: () => ed.cm,
    view: () => (ed.boundText ? ed.view : null),
    path: () => (ed.boundText ? ed.openPath : null),
    text: () => ed.boundText,
    canEdit: ed.canEdit,
    onChange: () => paintSpell(ed),
  });

  el.host = h("div", { class: "editor-host" }, ed.comments.floatEl, ed.grammar.box);
  el.other = h("div", { class: "editor-other" },
    h("div", { class: "loading" }, spinner(true)));
  el.host.hidden = true;
  const role = ed.p.role || "viewer";
  const readOnly = ed.canEdit ? null : h("div", { class: "editor-banner" }, icon("eye"),
    h("span", { text: `You are a ${role} of this paper, so the editor is read-only. The owner can make you an editor.` }));

  el.main = h("section", { class: "editor-main", "aria-label": "Editor" },
    h("div", { class: "editor-toolbar" },
      el.filesBtn, el.path, el.note, el.status, el.presence, el.spell, ed.comments.button, el.pdfBtn),
    readOnly,
    h("div", { class: "editor-pane" }, el.host, el.other));
  el.pdfColumn = h("div", { class: "editor-pdf" }, ed.comments.el);
  el.divider = h("div", {
    class: "editor-divider", role: "separator", "aria-orientation": "vertical", "aria-label": "Width of the PDF",
    tabindex: "0", title: "Drag to change the width. A double-click makes both halves equal.",
  });
  el.treeDivider = h("div", {
    class: "editor-divider editor-tree-divider", role: "separator", "aria-orientation": "vertical", "aria-label": "Width of the file tree",
    tabindex: "0", title: "Drag to change the width. A double-click gives the files their usual width.",
  });
  el.shell = h("div", { class: `editor-shell${stored(PDF_KEY, "on") === "off" ? " pdf-off" : ""}` },
    el.tree, el.treeDivider, el.main, el.divider, el.pdfColumn);
  el.pdfColumn.style.flexBasis = `${Number(stored(PDF_WIDTH_KEY, "0.42")) * 100}%`;
  const treeWidth = Number(stored(TREE_WIDTH_KEY, ""));
  if (treeWidth > 0) el.shell.style.setProperty("--tree-width", `${treeWidth}px`);
  wireDivider(ed);
  wireTreeDivider(ed);
  paintPdfButton(ed);
  // A file dropped next to the tree would make the browser leave the page.
  el.shell.addEventListener("dragover", (e) => {
    if ([...e.dataTransfer.types].includes("Files")) e.preventDefault();
  });
  el.shell.addEventListener("drop", (e) => e.preventDefault());
  return ed;
}

async function start(ed, want) {
  const cm = await loadBundle();
  if (!ed.ctx.alive()) return;
  ed.cm = cm;
  const doc = new cm.Y.Doc();
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const provider = new cm.WebsocketProvider(`${scheme}//${location.host}/api/projects`, `${ed.p.id}/sync`, doc, {
    resyncInterval: RESYNC_MS,
    // Every tab talks to the server itself, so each one shows the truth.
    disableBc: true,
    shouldReconnect: (e) => {
      if (e.code === ROLE_CHANGED && !ed.roleNoticeShown) {
        ed.roleNoticeShown = true;
        flash(h("span", {}, "Your role on this paper changed. ",
          h("a", { href: location.pathname, "data-external": "", text: "Reload the page" }), " to see what you can do now."), "warn");
      }
      return !CLOSED[e.code] && !(e.code >= 4400 && e.code < 4500);
    },
  });
  Object.assign(ed, { doc, provider, awareness: provider.awareness, files: doc.getMap(FILES) });
  ed.langComp = new cm.Compartment();
  ed.spellComp = new cm.Compartment();
  onLeave(() => destroy(ed));

  const me = session.me || {};
  const color = colorFor(me.email);
  ed.awareness.setLocalStateField("user", { name: me.name || me.email || "Someone", email: me.email || "", color, colorLight: `${color}33` });

  provider.on("status", () => paintStatus(ed));
  provider.on("sync", (synced) => {
    if (synced) {
      ed.everSynced = true;
      ed.unsent = false;
      if (!ed.ready) ready(ed, want);
    }
    paintStatus(ed);
  });
  provider.on("closed", ({ code }) => {
    ed.closedText = CLOSED[code] || "The server refused this page's changes. Reload the page.";
    paintStatus(ed);
    flash(h("span", {}, ed.closedText, " ",
      code === 4001 ? h("a", { href: `/login?next=${encodeURIComponent(location.pathname)}`, "data-external": "", text: "Sign in" }) : null), "error");
  });
  ed.awareness.on("change", () => paintPresence(ed));
  ed.index = new ProjectIndex();
  ed.onFiles = (events) => {
    ed.index.changed(events);
    ed.outline.changed();
    // Typing changes a Y.Text. Only entries coming, going or renamed change the tree.
    if (events.some((e) => !(e.target instanceof cm.Y.Text))) queueRefresh(ed);
  };
  ed.files.observeDeep(ed.onFiles);
  doc.on("update", (_update, origin) => {
    if (origin !== provider && !provider.wsconnected) ed.unsent = true;
    // Anyone's edit changes the PDF. The first sync is not an edit.
    if (ed.ready) {
      ed.pdf.edited(origin !== provider);
      ed.comments.edited();
    }
  });

  listen(ed, window, "online", () => paintStatus(ed));
  listen(ed, window, "offline", () => paintStatus(ed));
  listen(ed, window, "beforeunload", (e) => {
    if (ed.unsent) e.preventDefault();
  });
  listen(ed, document, "keydown", (e) => {
    if (e.defaultPrevented || !(e.ctrlKey || e.metaKey) || e.altKey) return;
    if (!e.shiftKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      note(ed, "Saved automatically");
    } else if (e.key === "Enter") {
      // The editor's own keymap has these when the text has the focus.
      e.preventDefault();
      if (e.shiftKey) ed.pdf.showCursor();
      else ed.pdf.compile();
    }
  });
  listen(ed, window, "resize", () => fit(ed));
  // Flashes above the page and a wrapping header move the editor down.
  const resized = new ResizeObserver(() => fit(ed));
  resized.observe(document.body);
  ed.offs.push(() => resized.disconnect());
  fit(ed);

  onPop((loc) => {
    const prefix = `${ed.base}/edit`;
    if (!ed.ready || (loc.pathname !== prefix && !loc.pathname.startsWith(`${prefix}/`))) return false;
    openPath(ed, decodePath(loc.pathname.slice(prefix.length + 1)), { history: null });
    return true;
  });
  paintStatus(ed);
}

function listen(ed, target, type, fn) {
  target.addEventListener(type, fn);
  ed.offs.push(() => target.removeEventListener(type, fn));
}

// Everything the page set up goes, the socket first, so nothing stays open
// after leaving the page.
function destroy(ed) {
  ed.dead = true;
  clearTimeout(ed.refreshTimer);
  clearTimeout(ed.noteTimer);
  ed.offs.forEach((off) => off());
  ed.view?.destroy();
  ed.view = null;
  ed.undo.forEach((um) => um.destroy());
  ed.files?.unobserveDeep(ed.onFiles);
  ed.provider?.destroy();
  ed.awareness?.destroy();
  ed.doc?.destroy();
}

// The editor fills the window below the project header.
function fit(ed) {
  const shell = ed.el.shell;
  if (!shell.isConnected) return;
  const top = shell.getBoundingClientRect().top + window.scrollY;
  const height = `${Math.max(320, Math.floor(window.innerHeight - top))}px`;
  if (shell.style.height !== height) shell.style.height = height;
}

// --- Status, people, notes ------------------------------------------------------

function paintStatus(ed) {
  const { provider, el } = ed;
  let kind;
  let text;
  if (ed.closedText) [kind, text] = ["error", ed.closedText];
  else if (provider?.wsconnected && ed.everSynced && navigator.onLine !== false) [kind, text] = ["ok", "Connected"];
  else if (!ed.everSynced) [kind, text] = ["pending", "Connecting…"];
  else [kind, text] = ["offline", "Offline, changes are kept and sent when the connection is back"];
  // Unchanged text is not written again, so screen readers announce changes only.
  if (el.statusText.textContent === text) return;
  el.status.className = `editor-status is-${kind}`;
  el.status.title = text;
  el.statusText.textContent = text;
}

function paintPresence(ed) {
  const me = session.me?.email || "";
  const people = new Map();
  ed.awareness.getStates().forEach((state, client) => {
    const user = state?.user;
    if (client === ed.doc.clientID || !user) return;
    // My other tabs are not someone else.
    if (user.email && user.email === me) return;
    const key = user.email || `client-${client}`;
    const where = ed.byId.get(state.file)?.shownAt || null;
    const known = people.get(key);
    if (!known) people.set(key, { name: user.name || user.email || "Someone", email: user.email || "", avatar_url: user.avatar_url || null, where });
    else if (!known.where) known.where = where;
  });
  // Every remote cursor move changes the awareness. Most change nothing here.
  const list = [...people.values()].sort((a, b) => a.name.localeCompare(b.name));
  const signature = JSON.stringify(list);
  if (signature === ed.presenceSignature) return;
  ed.presenceSignature = signature;
  const el = ed.el.presence;
  clear(el);
  el.hidden = people.size === 0;
  if (!people.size) return;
  el.setAttribute("aria-label", `Also here: ${list.map((p) => p.name).join(", ")}`);
  for (const p of list.slice(0, 4)) {
    el.append(h("span", { class: "editor-person", title: p.where ? `${p.name} · ${p.where}` : p.name },
      avatar(p, 20),
      list.length <= 2 ? h("span", { class: "editor-person-name", text: p.name }) : null));
  }
  if (list.length > 4) el.append(h("span", { class: "counter", title: list.slice(4).map((p) => p.name).join(", "), text: `+${list.length - 4}` }));
}

function note(ed, text) {
  const el = ed.el.note;
  el.textContent = text;
  el.classList.add("is-on");
  clearTimeout(ed.noteTimer);
  ed.noteTimer = setTimeout(() => {
    el.classList.remove("is-on");
    // Once it has faded, the note gives its room in the toolbar back.
    ed.noteTimer = setTimeout(() => { el.textContent = ""; }, 300);
  }, 2500);
}

function toggleSpellcheck(ed) {
  // With a checker on the server it is switched for the account (editor-grammar.js).
  if (ed.grammar.active) {
    ed.grammar.toggle();
    return;
  }
  ed.spell = !ed.spell;
  try {
    localStorage.setItem(SPELLCHECK_KEY, ed.spell ? "on" : "off");
  } catch {
    // Private windows may refuse. The toggle still works for this page.
  }
  ed.el.spell.setAttribute("aria-pressed", String(ed.spell));
  ed.view?.dispatch({ effects: ed.spellComp.reconfigure(spellAttrs(ed)) });
}

// The browser's spellchecker stays off while the server's checker is there.
// It would underline the same words again, in its own colour and with its own
// idea of the language, and LaTeX's commands with them.
function spellAttrs(ed) {
  return ed.cm.EditorView.contentAttributes.of({ spellcheck: ed.spell && !ed.grammar.active ? "true" : "false" });
}

// The Spellcheck button. With a checker on the server it shows whether that
// is on for the account and how many underlines the open file has, and its
// title names the language or the checker's last error. Without one, or for
// someone who may not edit, it is the browser's spellchecker as before.
function paintSpell(ed) {
  const g = ed.grammar;
  const btn = ed.el.spell;
  btn.setAttribute("aria-pressed", String(g.active ? g.enabled : ed.spell));
  btn.title = g.active ? g.title : "Underline misspelled words with the browser's spellchecker";
  ed.el.spellCount.textContent = String(g.count);
  ed.el.spellCount.hidden = !g.active || !g.count;
  btn.classList.toggle("is-busy", g.active && g.busy);
  btn.classList.toggle("is-failed", g.active && g.failed);
  if (ed.spellActive !== g.active) {
    ed.spellActive = g.active;
    if (ed.spellComp) ed.view?.dispatch({ effects: ed.spellComp.reconfigure(spellAttrs(ed)) });
  }
}

// --- The document -----------------------------------------------------------------

function ready(ed, want) {
  ed.ready = true;
  ed.comments.start();
  refresh(ed);
  ed.problems.ready();
  ed.outline.changed();
  ed.pdf.filesLoaded();
  // /edit/<path>#L<n>, the References tab's links, opens the file at that line.
  const at = /^#L(\d+)$/.exec(location.hash);
  openPath(ed, want, { history: "replace" });
  if (at && want && ed.openPath === want) openAt(ed, { path: want, line: Number(at[1]) });
}

function defaultFile(ed) {
  const main = ed.byPath.get(ed.p.main_file || "main.tex");
  if (main?.kind === "text") return main;
  return [...ed.byPath.values()]
    .filter((e) => e.kind === "text" && /\.tex$/i.test(e.shownAt))
    .sort((a, b) => a.shownAt.localeCompare(b.shownAt))[0] || null;
}

// Open `path`, or the default file when it is empty.
function openPath(ed, path, { history }) {
  const entry = path ? ed.byPath.get(path) : defaultFile(ed);
  if (entry) open(ed, entry.id, { history });
  else if (path) showMissing(ed, path);
  else showEmpty(ed);
}

function queueRefresh(ed) {
  if (ed.refreshTimer || ed.dead) return;
  ed.refreshTimer = setTimeout(() => {
    ed.refreshTimer = null;
    refresh(ed);
  }, 20);
}

// The file list changed. Rebuild the tree and follow the open file.
function refresh(ed) {
  if (ed.dead) return;
  ed.byPath = materialize(ed.files, ed.cm.Y);
  ed.byId = new Map([...ed.byPath.values()].map((e) => [e.id, e]));
  if (ed.pendingOpen && ed.byId.has(ed.pendingOpen)) {
    const id = ed.pendingOpen;
    ed.pendingOpen = null;
    open(ed, id, { history: "push", focus: true });
  } else if (ed.openId) {
    follow(ed);
  } else if (ed.ready && !ed.openPath && ed.waiting) {
    // The page is empty or names a missing file. That file may have just arrived.
    const entry = ed.waiting === true ? defaultFile(ed) : ed.byPath.get(ed.waiting);
    if (entry) open(ed, entry.id, { history: "replace" });
    else if (ed.waiting === true && ed.emptyHadFiles !== ed.byPath.size > 0) showEmpty(ed);
  }
  paintTree(ed);
  paintPresence(ed);
}

function follow(ed) {
  const entry = ed.byId.get(ed.openId);
  if (!entry) {
    gone(ed);
    return;
  }
  if (entry.shownAt !== ed.openPath) {
    const from = ed.openPath;
    const own = ed.ownMove && (from === ed.ownMove.from || from.startsWith(`${ed.ownMove.from}/`));
    if (own) ed.ownMove = null;
    ed.openPath = entry.shownAt;
    setUrl(ed, entry.shownAt, "replace");
    setTitle(entry.shownAt, ed.p.name);
    paintPath(ed, entry);
    if (entry.kind === "text") ed.view?.dispatch({ effects: ed.langComp.reconfigure(language(ed, entry.shownAt)) });
    if (entry.conflict && entry.path === from) note(ed, `Someone else made ${from} at the same time. This one is now a conflict copy.`);
    else note(ed, own ? `Renamed to ${entry.shownAt}` : `Someone renamed it to ${entry.shownAt}`);
  } else {
    paintPath(ed, entry);
  }
  if (entry.kind === "blob" && entry.sha !== ed.shownSha) showBlob(ed, entry);
  if (entry.kind === "text" && entry.text !== ed.boundText) bindText(ed, entry);
}

// The open file left the document.
function gone(ed) {
  const path = ed.openPath;
  const own = ed.ownDelete && (path === ed.ownDelete || path.startsWith(`${ed.ownDelete}/`));
  ed.boundText = null; // its text is gone, so there is no position to keep
  unbind(ed);
  ed.openId = null;
  ed.openPath = null;
  const same = ed.byPath.get(path);
  if (same) {
    // Deleted and created again at once, as a synced folder does with a
    // file that was replaced.
    open(ed, same.id, { history: "replace" });
    note(ed, `${path} was replaced`);
    return;
  }
  if (own) {
    ed.ownDelete = null;
    openPath(ed, "", { history: "replace" });
    return;
  }
  const main = defaultFile(ed);
  showMessage(ed, blankslate({
    iconName: "trash",
    title: `${path} was deleted`,
    text: "Someone removed it while you had it open. Its last snapshot is still in the history. Pick another file.",
    action: main ? h("a", { class: "btn", href: editorHref(ed.base, main.shownAt), onclick: (e) => openFromLink(ed, e, main.id) }, `Open ${main.shownAt}`) : null,
  }));
  setTitle("Editor", ed.p.name);
  paintPath(ed, null);
  showTree(ed, true);
}

function setUrl(ed, path, mode) {
  if (!mode) return;
  const url = editorHref(ed.base, path);
  if (location.pathname === url) return;
  if (mode === "push") history.pushState({}, "", url);
  else history.replaceState(history.state, "", url);
}

function open(ed, id, { history = "push", focus = false } = {}) {
  const entry = ed.byId.get(id);
  if (!entry) return;
  ed.waiting = null;
  if (id !== ed.openId) {
    saveSelection(ed);
    ed.boundText = null;
    ed.openId = id;
    ed.openPath = entry.shownAt;
    ed.awareness.setLocalStateField("file", id);
    if (entry.kind === "text") bindText(ed, entry);
    else showBlob(ed, entry);
    paintTree(ed);
  }
  setUrl(ed, entry.shownAt, history);
  setTitle(entry.shownAt, ed.p.name);
  paintPath(ed, entry);
  ed.outline.moved();
  showTree(ed, false);
  if (focus && ed.view && window.matchMedia("(min-width: 768px)").matches) ed.view.focus();
}

function openFromLink(ed, e, id) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  open(ed, id, { history: "push", focus: true });
}

function paintPath(ed, entry) {
  const el = clear(ed.el.path);
  if (!entry) return;
  const parts = entry.shownAt.split("/");
  const name = parts.pop();
  el.title = entry.shownAt;
  append(el, [icon(entry.kind === "blob" ? "file-binary" : "file-text", { cls: "color-muted" }),
    h("span", { class: "truncate" },
      parts.length ? h("span", { class: "color-muted", text: `${parts.join(" / ")} / ` }) : null,
      h("strong", { text: name })),
    entry.conflict ? h("span", {
      class: "Label label-attention", text: "Conflict copy",
      title: `Someone else created ${entry.path} at the same time. Their file keeps the name. This one is shown as ${entry.shownAt}. Merge the two and delete one.`,
    }) : null]);
}

// --- Text files -----------------------------------------------------------------

// LaTeX highlighting, and for those who may edit, completions.
function language(ed, path) {
  if (!LATEX.test(path)) return [];
  ed.latex ??= [
    ed.cm.StreamLanguage.define(ed.cm.stex),
    ed.canEdit ? latexAutocomplete(ed.cm, () => ed.index.read(ed.byPath)) : [],
  ];
  return ed.latex;
}

function bindText(ed, entry) {
  const cm = ed.cm;
  if (!entry.text) {
    showMessage(ed, blankslate({ iconName: "alert", title: "This file cannot be opened", text: "Its entry in the live document has no text." }));
    return;
  }
  ed.awareness.setLocalStateField("cursor", null);
  let undoManager = ed.undo.get(entry.id);
  if (!undoManager) {
    undoManager = new cm.Y.UndoManager(entry.text);
    ed.undo.set(entry.id, undoManager);
  }
  const doc = entry.text.toString();
  const selection = restoreSelection(ed, entry, doc.length);
  const state = cm.EditorState.create({
    doc,
    selection,
    extensions: [
      cm.lineNumbers(),
      ed.problems.extension(),
      cm.highlightActiveLineGutter(),
      cm.highlightSpecialChars(),
      cm.drawSelection(),
      cm.dropCursor(),
      cm.indentOnInput(),
      cm.bracketMatching(),
      cm.highlightActiveLine(),
      cm.highlightSelectionMatches(),
      cm.search({ top: true }),
      cm.EditorView.lineWrapping,
      cm.indentUnit.of("  "),
      cm.EditorState.readOnly.of(!ed.canEdit),
      cm.EditorView.contentAttributes.of({ "aria-label": `Contents of ${entry.shownAt}` }),
      ed.langComp.of(language(ed, entry.shownAt)),
      ed.spellComp.of(spellAttrs(ed)),
      // Before the keymap below, so Escape closes the suggestions first.
      ed.grammar.extension(),
      cm.keymap.of([
        { key: "Mod-Enter", run: () => { ed.pdf.compile(); return true; } },
        { key: "Mod-Shift-Enter", run: () => { ed.pdf.showCursor(); return true; } },
        ...cm.yUndoManagerKeymap, ...cm.searchKeymap, ...cm.defaultKeymap,
        { key: "Tab", run: cm.acceptCompletion }, cm.indentWithTab]),
      // CodeMirror would paste a dropped file's bytes as text. Upload it instead.
      cm.EditorView.domEventHandlers({
        drop: (e) => {
          if (!e.dataTransfer?.files?.length) return false;
          e.preventDefault();
          if (ed.canEdit) upload(ed, [...e.dataTransfer.files], parentOf(ed.openPath || ""));
          return true;
        },
      }),
      editorLooks(cm),
      ed.comments.extension(),
      ed.outline.extension(),
      spotExtension(cm),
      cm.yCollab(entry.text, ed.awareness, { undoManager }),
    ],
  });
  ed.el.other.hidden = true;
  ed.el.host.hidden = false;
  ed.shownSha = null;
  if (ed.view) ed.view.setState(state);
  else ed.view = new cm.EditorView({ state, parent: ed.el.host });
  ed.boundText = entry.text;
  ed.comments.opened();
  ed.problems.opened();
  ed.grammar.opened();
  if (selection) reveal(ed, selection.main.head, false);
}

// Scroll the editor so `pos` is in its middle, and put the cursor there. Only
// the editor scrolls. CodeMirror's own "center" would center the page in the
// window as well.
function reveal(ed, pos, select = true) {
  const view = ed.view;
  view.dispatch({
    selection: select ? { anchor: pos } : undefined,
    effects: ed.cm.EditorView.scrollIntoView(pos, { y: "nearest", yMargin: 40 }),
  });
  requestAnimationFrame(() => ed.view === view && view.requestMeasure({
    read: () => {
      const at = view.coordsAtPos(pos);
      const box = view.scrollDOM.getBoundingClientRect();
      return at ? at.top - box.top - box.height / 2 : 0;
    },
    write: (offset) => { view.scrollDOM.scrollTop += offset; },
  }));
}

function unbind(ed) {
  saveSelection(ed);
  ed.view?.destroy();
  ed.view = null;
  ed.boundText = null;
  ed.awareness?.setLocalStateField("cursor", null);
  ed.comments?.opened();
  ed.grammar?.opened();
}

// Where the cursor was in each file, as positions that move with edits.
function saveSelection(ed) {
  if (!ed.view || !ed.boundText || !ed.openId) return;
  const { Y } = ed.cm;
  const s = ed.view.state.selection.main;
  ed.selections.set(ed.openId, {
    anchor: Y.createRelativePositionFromTypeIndex(ed.boundText, s.anchor),
    head: Y.createRelativePositionFromTypeIndex(ed.boundText, s.head),
  });
}

function restoreSelection(ed, entry, length) {
  const saved = ed.selections.get(entry.id);
  if (!saved) return undefined;
  const { Y, EditorSelection } = ed.cm;
  const at = (rel) => {
    const abs = Y.createAbsolutePositionFromRelativePosition(rel, ed.doc);
    return abs && abs.type === entry.text ? Math.min(abs.index, length) : null;
  };
  const anchor = at(saved.anchor);
  const head = at(saved.head);
  return anchor === null || head === null ? undefined : EditorSelection.single(anchor, head);
}

// White like the rest of the app, the Files tab's monospace font, and syntax
// colours from GitHub's light theme.
function editorLooks(cm) {
  if (looks) return looks;
  const t = cm.tags;
  looks = [
    cm.EditorView.theme({
      "&": { height: "100%", fontSize: "13px", color: "var(--fg)", backgroundColor: "var(--bg)" },
      "&.cm-focused": { outline: "none" },
      ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "20px" },
      ".cm-content": { padding: "8px 0 48px", caretColor: "var(--fg)" },
      ".cm-line": { padding: "0 16px 0 8px" },
      ".cm-cursor, .cm-dropCursor": { borderLeft: "2px solid var(--fg)" },
      ".cm-gutters": { color: "var(--fg-subtle)", backgroundColor: "var(--bg)", border: "none", borderRight: "1px solid var(--border-muted)" },
      ".cm-lineNumbers .cm-gutterElement": { minWidth: "44px", padding: "0 10px 0 12px" },
      ".cm-activeLine": { backgroundColor: "#eaeef259" },
      ".cm-activeLineGutter": { color: "var(--fg)", backgroundColor: "#eaeef259" },
      ".cm-selectionBackground": { backgroundColor: "#dde3ea" },
      "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground": { backgroundColor: "#c8e1ff" },
      ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": { color: "inherit", backgroundColor: "#dafbe1", outline: "1px solid #4ac26b99" },
      ".cm-nonmatchingBracket, &.cm-focused .cm-nonmatchingBracket": { color: "inherit", backgroundColor: "#ffebe9" },
      ".cm-selectionMatch": { backgroundColor: "#ddf4ff" },
      ".cm-searchMatch": { backgroundColor: "#fff8c5", outline: "1px solid #d4a72c" },
      ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "#ffdf5d" },
      ".cm-panels": { color: "var(--fg)", backgroundColor: "var(--bg-subtle)" },
      ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border-muted)" },
      ".cm-panel.cm-search": { display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px", padding: "8px 44px 8px 12px", fontFamily: "var(--font)", fontSize: "12px" },
      ".cm-panel.cm-search br": { display: "none" },
      ".cm-panel.cm-search label": { display: "inline-flex", alignItems: "center", gap: "4px", margin: "0", fontSize: "12px" },
      ".cm-panel.cm-search [name=close]": { top: "10px", right: "10px", width: "24px", height: "24px", padding: "0", fontSize: "18px", lineHeight: "22px", color: "var(--fg-muted)", borderRadius: "6px" },
      ".cm-panel.cm-search [name=close]:hover": { backgroundColor: "var(--bg-muted)" },
      ".cm-textfield": { height: "28px", margin: "0", padding: "3px 8px", font: "inherit", fontSize: "12px", color: "var(--fg)", backgroundColor: "var(--bg)", border: "1px solid var(--border)", borderRadius: "6px" },
      ".cm-textfield:focus": { outline: "none", borderColor: "var(--accent)", boxShadow: "inset 0 0 0 1px var(--accent)" },
      ".cm-button": { height: "28px", margin: "0", padding: "0 10px", font: "inherit", fontSize: "12px", fontWeight: "500", color: "var(--fg)", backgroundColor: "var(--bg-subtle)", backgroundImage: "none", border: "1px solid var(--border)", borderRadius: "6px", cursor: "pointer" },
      ".cm-button:hover": { backgroundColor: "var(--bg-muted)" },
      ".cm-button:active": { backgroundColor: "#e6eaef", backgroundImage: "none" },
      ".cm-tooltip": { color: "var(--fg)", backgroundColor: "var(--bg)", border: "none", borderRadius: "8px", boxShadow: "var(--shadow-overlay)" },
      ".cm-tooltip.cm-tooltip-autocomplete > ul": { maxHeight: "18em", padding: "4px", fontFamily: "var(--mono)", fontSize: "12px" },
      ".cm-tooltip.cm-tooltip-autocomplete > ul > li": { padding: "3px 8px", lineHeight: "18px", borderRadius: "4px" },
      ".cm-tooltip.cm-tooltip-autocomplete > ul > li[aria-selected]": { color: "var(--fg)", backgroundColor: "var(--accent-bg)" },
      ".cm-completionMatchedText": { textDecoration: "none", fontWeight: "700" },
      ".cm-completionDetail": { marginLeft: "12px", fontFamily: "var(--font)", fontStyle: "normal", color: "var(--fg-muted)" },
      ".cm-tooltip.cm-completionInfo": { maxWidth: "360px", padding: "8px 10px", fontFamily: "var(--font)", fontSize: "12px", whiteSpace: "pre-line" },
      ".cm-ySelectionInfo": { top: "-17px", padding: "0 5px", fontFamily: "var(--font)", fontSize: "11px", fontWeight: "600", lineHeight: "17px", borderRadius: "4px 4px 4px 0" },
    }),
    cm.syntaxHighlighting(cm.HighlightStyle.define([
      { tag: t.comment, color: "#6e7781", fontStyle: "italic" },
      { tag: t.tagName, color: "#0550ae" },
      { tag: t.atom, color: "#953800" },
      { tag: [t.keyword, t.special(t.variableName), t.number], color: "#116329" },
      { tag: t.bracket, color: "#59636e" },
      { tag: t.string, color: "#0a3069" },
      { tag: t.invalid, color: "#d1242f" },
    ])),
    // The search panel's labels, written like the rest of the app.
    cm.EditorState.phrases.of({
      "next": "Next", "previous": "Previous", "all": "All", "match case": "Match case", "regexp": "Regexp",
      "by word": "By word", "replace": "Replace", "replace all": "Replace all", "close": "Close", "Replace": "Replace with",
    }),
  ];
  return looks;
}

// --- Other views in the pane ------------------------------------------------------

function showMessage(ed, node) {
  unbind(ed);
  ed.shownSha = null;
  ed.el.host.hidden = true;
  ed.el.other.hidden = false;
  clear(ed.el.other).append(h("div", { class: "editor-message" }, node));
}

function showBlob(ed, entry) {
  unbind(ed);
  const url = `${projectUrl(ed.p.id, `/files/${encPath(entry.shownAt)}`)}?v=${encodeURIComponent((entry.sha || "").slice(0, 12))}`;
  const name = entry.shownAt.split("/").pop();
  const download = h("a", { class: "btn btn-sm", href: url, download: name }, icon("download"), "Download");
  let body;
  if (IMAGE.test(name)) {
    body = h("div", { class: "image-preview editor-image" }, h("img", { src: url, alt: entry.shownAt }));
  } else if (/\.pdf$/i.test(name)) {
    body = blankslate({
      iconName: "pdf", title: name, text: `A PDF of ${formatBytes(entry.size)}.`,
      action: h("div", { class: "flex", style: { justifyContent: "center" } },
        h("a", { class: "btn", href: url, target: "_blank", rel: "noopener" }, "Open the PDF"),
        h("a", { class: "btn", href: url, download: name }, icon("download"), "Download")),
    });
  } else {
    body = blankslate({
      iconName: "file-binary", title: name, text: `A binary file of ${formatBytes(entry.size)}. It cannot be edited here.`,
      action: h("a", { class: "btn", href: url, download: name }, icon("download"), "Download"),
    });
  }
  ed.el.host.hidden = true;
  ed.el.other.hidden = false;
  clear(ed.el.other).append(h("div", { class: "editor-blob" },
    h("div", { class: "editor-blob-head" }, h("span", { class: "color-muted", text: `${formatBytes(entry.size)} · binary file` }), h("span", { class: "flex-1" }), download),
    body));
  ed.shownSha = entry.sha;
}

function showEmpty(ed) {
  ed.openId = null;
  ed.openPath = null;
  ed.waiting = true;
  ed.awareness.setLocalStateField("file", null);
  const hasFiles = ed.byPath.size > 0;
  ed.emptyHadFiles = hasFiles;
  const actions = ed.canEdit ? h("div", { class: "flex", style: { justifyContent: "center" } },
    h("button", { type: "button", class: "btn btn-primary", onclick: () => newFile(ed, "", hasFiles ? "" : ed.p.main_file || "main.tex") }, icon("plus"), "New file"),
    h("button", { type: "button", class: "btn", onclick: () => pickUpload(ed, "") }, icon("upload"), "Upload files")) : null;
  showMessage(ed, blankslate({
    iconName: "file-text",
    title: hasFiles ? "Pick a file to edit" : "No files yet",
    text: hasFiles ? "There is no .tex file yet. Open a file from the list, or start one."
      : ed.canEdit ? "Start with the main file, or upload what you have. Your coauthors see every keystroke." : "Nobody has added a file yet.",
    action: actions,
  }));
  setTitle("Editor", ed.p.name);
  paintPath(ed, null);
  paintTree(ed);
}

function showMissing(ed, path) {
  ed.openId = null;
  ed.openPath = null;
  ed.waiting = path;
  ed.awareness.setLocalStateField("file", null);
  const main = defaultFile(ed);
  showMessage(ed, blankslate({
    iconName: "file",
    title: "No such file",
    text: `There is no ${path} in this paper.`,
    action: main ? h("a", { class: "btn", href: editorHref(ed.base, main.shownAt), onclick: (e) => openFromLink(ed, e, main.id) }, `Open ${main.shownAt}`) : null,
  }));
  setTitle(path, ed.p.name);
  paintPath(ed, null);
  paintTree(ed);
}

// --- The file tree --------------------------------------------------------------------

function showTree(ed, on) {
  ed.el.shell.classList.toggle("show-tree", on);
  ed.el.filesBtn.setAttribute("aria-expanded", String(on));
}

// --- The PDF next to the text -------------------------------------------------------

// On a wide screen the PDF is a column that can be hidden. On a phone it takes
// the place of the text.
function pdfShown(ed) {
  const shell = ed.el.shell;
  if (shell.classList.contains("show-comments")) return false;
  return window.matchMedia(PHONE).matches ? shell.classList.contains("show-pdf") : !shell.classList.contains("pdf-off");
}

function paintPdfButton(ed) {
  ed.el.pdfBtn.setAttribute("aria-pressed", String(pdfShown(ed)));
}

function togglePdf(ed, on = !pdfShown(ed)) {
  const shell = ed.el.shell;
  if (shell.classList.contains("show-comments")) showComments(ed, false, { keepColumn: on });
  if (window.matchMedia(PHONE).matches) {
    shell.classList.toggle("show-pdf", on);
  } else {
    shell.classList.toggle("pdf-off", !on);
    store(PDF_KEY, on ? "on" : "off");
  }
  paintPdfButton(ed);
}

// The comments take the PDF's column while they are shown. Hiding them gives
// the column back to the PDF, or hides it when it was hidden before.
function showComments(ed, on, { keepColumn = false } = {}) {
  const shell = ed.el.shell;
  const phone = window.matchMedia(PHONE).matches;
  if (on && !shell.classList.contains("show-comments")) {
    ed.columnWas = phone ? shell.classList.contains("show-pdf") : !shell.classList.contains("pdf-off");
    if (phone) shell.classList.add("show-pdf");
    else shell.classList.remove("pdf-off");
  } else if (!on && !keepColumn && !ed.columnWas) {
    if (phone) shell.classList.remove("show-pdf");
    else shell.classList.add("pdf-off");
  }
  shell.classList.toggle("show-comments", on);
  ed.comments.shown(on);
  paintPdfButton(ed);
}

// While a divider is dragged, `drag` gets the pointer's x.
function dragDivider(ed, divider, drag) {
  const shell = ed.el.shell;
  divider.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    divider.setPointerCapture(e.pointerId);
    shell.classList.add("is-resizing");
    divider.classList.add("is-dragging");
    const move = (m) => drag(m.clientX);
    const up = () => {
      shell.classList.remove("is-resizing");
      divider.classList.remove("is-dragging");
      divider.removeEventListener("pointermove", move);
      divider.removeEventListener("pointerup", up);
      divider.removeEventListener("pointercancel", up);
    };
    divider.addEventListener("pointermove", move);
    divider.addEventListener("pointerup", up);
    divider.addEventListener("pointercancel", up);
  });
}

function wireDivider(ed) {
  const { divider, pdfColumn, shell } = ed.el;
  const setWidth = (px) => {
    const room = shell.clientWidth;
    const text = ed.el.main.getBoundingClientRect().width + pdfColumn.getBoundingClientRect().width;
    const width = Math.min(Math.max(240, px), text - 320);
    pdfColumn.style.flexBasis = `${(width / room) * 100}%`;
    store(PDF_WIDTH_KEY, String(width / room));
  };
  dragDivider(ed, divider, (x) => setWidth(shell.getBoundingClientRect().right - x));
  divider.addEventListener("dblclick", () => setWidth((ed.el.main.offsetWidth + pdfColumn.offsetWidth) / 2));
  divider.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    setWidth(pdfColumn.offsetWidth + (e.key === "ArrowLeft" ? 32 : -32));
  });
}

// The tree keeps its width in pixels, at most half the editor, and leaves the
// text at least 320 pixels. A double-click gives it the stylesheet's width back.
function wireTreeDivider(ed) {
  const { treeDivider, tree, main, shell } = ed.el;
  const setWidth = (px) => {
    const room = tree.offsetWidth + main.offsetWidth;
    const width = Math.round(Math.max(160, Math.min(px, room - 320, shell.clientWidth / 2)));
    shell.style.setProperty("--tree-width", `${width}px`);
    store(TREE_WIDTH_KEY, String(width));
  };
  dragDivider(ed, treeDivider, (x) => setWidth(x - tree.getBoundingClientRect().left));
  treeDivider.addEventListener("dblclick", () => {
    shell.style.removeProperty("--tree-width");
    store(TREE_WIDTH_KEY, "");
  });
  treeDivider.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    setWidth(tree.offsetWidth + (e.key === "ArrowLeft" ? -32 : 32));
  });
}

// Show a place the PDF pointed at: a problem's line or a SyncTeX result. A
// SyncTeX result has `spot` ({word, before, after} of the click), and
// its line and word light up for a moment (editor-spot.js).
function openAt(ed, { path, line, column = 0, spot = null }) {
  const entry = ed.byPath.get(path);
  if (!entry || entry.kind !== "text") {
    note(ed, ed.ready ? `${path} is not a text file of this paper` : "The files are still loading");
    return;
  }
  if (window.matchMedia(PHONE).matches) togglePdf(ed, false);
  open(ed, entry.id, { history: entry.id === ed.openId ? null : "push" });
  const view = ed.view;
  if (!view) return;
  const doc = view.state.doc;
  const target = doc.line(Math.max(1, Math.min(line || 1, doc.lines)));
  if (spot) reveal(ed, showSpot(view, ed.cm, { line: target.number, ...spot }), false);
  else reveal(ed, target.from + Math.max(0, Math.min(column, target.length)));
  if (!window.matchMedia(PHONE).matches) view.focus();
}

function cursorPlace(ed) {
  if (!ed.view || !ed.boundText || !ed.openPath) return null;
  const head = ed.view.state.selection.main.head;
  const line = ed.view.state.doc.lineAt(head);
  return { path: ed.openPath, line: line.number, column: head - line.from };
}

function paintTree(ed) {
  const body = ed.el.treeBody;
  if (!ed.ready) return;
  const root = { dirs: new Map(), files: [] };
  for (const entry of ed.byPath.values()) {
    const parts = entry.shownAt.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!node.dirs.has(parts[i])) node.dirs.set(parts[i], { name: parts[i], path: parts.slice(0, i + 1).join("/"), dirs: new Map(), files: [] });
      node = node.dirs.get(parts[i]);
    }
    node.files.push({ name: parts[parts.length - 1], entry });
  }
  if (!ed.byPath.size) {
    clear(body).append(h("p", { class: "editor-tree-empty", text: ed.canEdit ? "No files yet. Drop files here to upload them." : "No files yet." }));
    return;
  }
  const list = h("ul", { class: "tree" });
  const walk = (node, depth) => {
    for (const dir of [...node.dirs.values()].sort(byName)) {
      const open = ed.expanded.has(dir.path);
      list.append(h("li", {}, dirRow(ed, dir, depth, open)));
      if (open) walk(dir, depth + 1);
    }
    for (const file of node.files.sort(byName)) list.append(h("li", {}, fileRow(ed, file, depth)));
  };
  walk(root, 0);
  clear(body).append(list);
}

function countFiles(dir) {
  let n = dir.files.length;
  for (const d of dir.dirs.values()) n += countFiles(d);
  return n;
}

function dirRow(ed, dir, depth, open) {
  const flip = () => {
    if (open) ed.expanded.delete(dir.path);
    else ed.expanded.add(dir.path);
    saveFolders(ed);
    paintTree(ed);
  };
  const label = open ? `Collapse ${dir.name}` : `Expand ${dir.name}`;
  const menu = ed.canEdit ? dropdown({
    summary: icon("kebab"),
    summaryClass: "btn-octicon",
    label: `Actions for folder ${dir.path}`,
    align: "right",
    cls: "tree-actions",
    items: [
      { text: "New file here…", icon: "file", onClick: () => newFile(ed, dir.path) },
      { text: "Upload files here…", icon: "upload", onClick: () => pickUpload(ed, dir.path) },
      { text: "Rename or move…", icon: "pencil", onClick: () => renameDialog(ed, dir.path, true) },
      "divider",
      { text: "Delete folder", icon: "trash", danger: true, onClick: () => removeDialog(ed, dir.path, countFiles(dir)) },
    ],
  }) : null;
  return h("div", {
    class: "tree-row", style: { paddingLeft: `${depth * 16}px` }, draggable: ed.canEdit ? "true" : null, dataset: { dir: dir.path },
  },
  h("button", { type: "button", class: "tree-toggle", "aria-label": label, "aria-expanded": String(open), onclick: flip },
    icon(open ? "chevron-down" : "chevron-right", { size: 12 })),
  h("button", { type: "button", class: "tree-link tree-dir", "aria-expanded": String(open), onclick: flip },
    icon("folder-fill", { cls: "dir" }), h("span", { class: "flex-1 truncate", text: dir.name })),
  menu);
}

function fileRow(ed, file, depth) {
  const entry = file.entry;
  const iconName = entry.kind === "text" ? "file-text" : /\.pdf$/i.test(entry.shownAt) ? "pdf" : "file-binary";
  const isMain = entry.shownAt === (ed.p.main_file || "main.tex");
  const canBeMain = !isMain && !entry.conflict && entry.kind === "text" && /\.tex$/i.test(entry.shownAt);
  const menu = ed.canEdit ? dropdown({
    summary: icon("kebab"),
    summaryClass: "btn-octicon",
    label: `Actions for ${entry.shownAt}`,
    align: "right",
    cls: "tree-actions",
    items: [
      canBeMain ? { text: "Make main document", icon: "book", onClick: () => makeMain(ed, entry.shownAt) } : null,
      { text: "Rename or move…", icon: "pencil", onClick: () => renameDialog(ed, entry.shownAt, false) },
      { text: "Download", icon: "download", href: projectUrl(ed.p.id, `/files/${encPath(entry.shownAt)}`), download: file.name },
      "divider",
      { text: "Delete", icon: "trash", danger: true, onClick: () => removeDialog(ed, entry.shownAt, 0) },
    ],
  }) : null;
  return h("div", {
    class: "tree-row", style: { paddingLeft: `${depth * 16}px` }, draggable: ed.canEdit ? "true" : null,
    "aria-current": entry.id === ed.openId ? "page" : null, dataset: { path: entry.shownAt },
  },
  h("span", { class: "tree-toggle spacer" }),
  h("a", { class: "tree-link", href: editorHref(ed.base, entry.shownAt), title: entry.shownAt, dataset: { id: entry.id } },
    icon(iconName), h("span", { class: "flex-1 truncate", text: file.name }),
    isMain ? h("span", { class: "Label", text: "main", title: "The main document, the file LaTeX compiles" }) : null,
    entry.conflict ? h("span", { class: "Label label-attention", text: "conflict" }) : null),
  menu);
}

// Clicks, drags and drops on the tree, in one place for every row.
function wireTree(ed) {
  const body = ed.el.treeBody;
  body.addEventListener("click", (e) => {
    const link = e.target.closest("a.tree-link[data-id]");
    if (link) openFromLink(ed, e, link.dataset.id);
  });
  if (!ed.canEdit) return;

  // The folder a drop lands in: a folder row, the folder of a file row, or the top.
  const folderAt = (target) => {
    const row = target.closest?.(".tree-row");
    if (row?.dataset.dir !== undefined) return row.dataset.dir;
    if (row?.dataset.path !== undefined) return parentOf(row.dataset.path);
    return "";
  };
  const unmark = () => {
    body.classList.remove("is-drop");
    body.querySelectorAll(".drop-target").forEach((r) => r.classList.remove("drop-target"));
  };
  const mark = (folder) => {
    unmark();
    if (!folder) body.classList.add("is-drop");
    else body.querySelector(`.tree-row[data-dir="${CSS.escape(folder)}"]`)?.classList.add("drop-target");
  };
  body.addEventListener("dragstart", (e) => {
    const row = e.target.closest?.(".tree-row");
    if (!row) return;
    e.dataTransfer.setData(DRAG_PATH, row.dataset.dir ?? row.dataset.path);
    e.dataTransfer.effectAllowed = "move";
    row.classList.add("dragging");
  });
  body.addEventListener("dragend", (e) => {
    e.target.closest?.(".tree-row")?.classList.remove("dragging");
    unmark();
  });
  body.addEventListener("dragover", (e) => {
    const types = [...e.dataTransfer.types];
    if (!types.includes("Files") && !types.includes(DRAG_PATH)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = types.includes("Files") ? "copy" : "move";
    mark(folderAt(e.target));
  });
  body.addEventListener("dragleave", (e) => {
    if (!body.contains(e.relatedTarget)) unmark();
  });
  body.addEventListener("drop", (e) => {
    e.preventDefault();
    unmark();
    const folder = folderAt(e.target);
    const moved = e.dataTransfer.getData(DRAG_PATH);
    if (moved) {
      const to = joinPath(folder, moved.split("/").pop());
      if (to !== moved && folder !== moved && !folder.startsWith(`${moved}/`)) move(ed, moved, to);
      return;
    }
    const items = [...(e.dataTransfer.items || [])];
    const folders = items.filter((i) => i.webkitGetAsEntry?.()?.isDirectory).length;
    if (folders) flash("Folders cannot be dropped yet. Make the folder with New folder, then drop its files on it.", "warn");
    const files = [...e.dataTransfer.files].filter((f, i) => !items[i]?.webkitGetAsEntry?.()?.isDirectory);
    upload(ed, files, folder);
  });
}

// --- Changes through the API ----------------------------------------------------------

function fileUrl(ed, path) {
  return projectUrl(ed.p.id, `/files/${encPath(path)}`);
}

// Why `path` cannot be created next to the files there are, or null.
function clash(ed, path, leaving = "") {
  const problem = pathProblem(path);
  if (problem) return problem;
  const moving = (p) => leaving && (p === leaving || p.startsWith(`${leaving}/`));
  if (ed.byPath.has(path) && !moving(path)) return `${path} exists already`;
  for (const p of ed.byPath.keys()) {
    if (moving(p)) continue;
    if (p.startsWith(`${path}/`)) return `${path} is a folder`;
    if (path.startsWith(`${p}/`)) return `${p} is a file, so it cannot hold ${path}`;
  }
  return null;
}

function starter(ed, path) {
  if (!/\.tex$/i.test(path)) return "";
  if (path === (ed.p.main_file || "main.tex")) return "\\documentclass{article}\n\n\\begin{document}\n\n\\end{document}\n";
  return "\\section{}\n";
}

async function createFile(ed, path) {
  const body = new Blob([starter(ed, path)], { type: "text/plain" });
  expand(ed, parentOf(path));
  let info;
  try {
    // Create only. A coauthor who made the same file a moment ago keeps theirs.
    info = await api.put(fileUrl(ed, path), body, {
      contentType: "text/plain; charset=utf-8", headers: { "If-None-Match": "*" },
    });
  } catch (err) {
    if (err.status !== 412) throw err;
    note(ed, `${path} was just created by someone else`);
    // Their file came over the socket before this answer, so it is in the tree.
    const entry = ed.byPath.get(path);
    if (entry) open(ed, entry.id, { history: "push", focus: true });
    return;
  }
  // The new entry comes back over the socket, before or after this answer.
  ed.pendingOpen = info?.id || null;
  queueRefresh(ed);
}

// A small form in a dialog. Resolves with the values, or null when cancelled.
function formDialog({ title, fields, confirmText, check }) {
  return new Promise((resolve) => {
    let result = null;
    const inputs = fields.map((f) => h("input", {
      type: "text", class: "form-control input-block input-mono", value: f.value || "", placeholder: f.placeholder || null,
      spellcheck: "false", autocapitalize: "off", autocomplete: "off",
    }));
    const error = h("div", { class: "form-error", role: "alert", hidden: true });
    inputs.forEach((i) => i.addEventListener("input", () => { error.hidden = true; }));
    const formId = `form-${Math.random().toString(36).slice(2)}`;
    const form = h("form", { id: formId }, fields.map((f, i) => field(f.label, inputs[i], { hint: f.hint })), error);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const values = inputs.map((i) => i.value.trim());
      const problem = check(values);
      if (problem) {
        error.textContent = problem;
        error.hidden = false;
        return;
      }
      result = values;
      d.close();
    });
    const d = openDialog({
      title,
      body: form,
      footer: [
        h("button", { type: "button", class: "btn", text: "Cancel", onclick: () => d.close() }),
        h("button", { type: "submit", class: "btn btn-primary", form: formId, text: confirmText }),
      ],
      onClose: () => resolve(result),
    });
    const first = inputs[0];
    first.focus();
    const sel = fields[0].select;
    if (sel) first.setSelectionRange(sel[0], sel[1]);
    else first.setSelectionRange(first.value.length, first.value.length);
  });
}

async function newFile(ed, folder, suggestion = "") {
  const prefix = folder ? `${folder}/` : "";
  const values = await formDialog({
    title: "New file",
    fields: [{ label: "Path", value: prefix + suggestion, placeholder: `${prefix}intro.tex`, hint: "A path with folders, like sections/intro.tex, makes the folders too." }],
    confirmText: "Create file",
    check: ([path]) => clash(ed, path),
  });
  if (!values) return;
  try {
    await createFile(ed, values[0]);
  } catch (err) {
    flashError(err);
  }
}

async function newFolder(ed, parent) {
  const prefix = parent ? `${parent}/` : "";
  const values = await formDialog({
    title: "New folder",
    fields: [
      { label: "Folder", value: prefix, placeholder: `${prefix}sections` },
      { label: "Its first file", value: "", placeholder: "intro.tex", hint: "A folder lives as long as it holds a file." },
    ],
    confirmText: "Create folder",
    check: ([dir, name]) => {
      if (!dir.replace(/\/+$/, "")) return "Name the folder.";
      if (!name) return "Name the first file in the folder.";
      if (name.includes("/")) return "The file name cannot contain a slash.";
      return clash(ed, `${dir.replace(/\/+$/, "")}/${name}`);
    },
  });
  if (!values) return;
  try {
    await createFile(ed, `${values[0].replace(/\/+$/, "")}/${values[1]}`);
  } catch (err) {
    flashError(err);
  }
}

async function renameDialog(ed, path, isDir) {
  const name = path.split("/").pop();
  const dot = isDir ? -1 : name.lastIndexOf(".");
  const start = path.length - name.length;
  const values = await formDialog({
    title: isDir ? `Rename or move ${path}/` : `Rename or move ${path}`,
    fields: [{
      label: "New path", value: path, select: [start, dot > 0 ? start + dot : path.length],
      hint: isDir ? "Every file in the folder moves with it." : "Change the folders in the path to move the file.",
    }],
    confirmText: isDir ? "Move folder" : "Rename",
    check: ([to]) => {
      if (to === path) return null;
      if (!isDir) return clash(ed, to, path);
      if (to.startsWith(`${path}/`)) return "A folder cannot move into itself.";
      // Every file in the folder moves. A folder of that name may exist already.
      const problem = pathProblem(to);
      if (problem) return problem;
      for (const p of ed.byPath.keys()) {
        const blocked = p.startsWith(`${path}/`) && clash(ed, to + p.slice(path.length), path);
        if (blocked) return blocked;
      }
      return null;
    },
  });
  if (!values || values[0] === path) return;
  await move(ed, path, values[0]);
}

async function move(ed, from, to) {
  ed.ownMove = { from, to };
  try {
    await api.post(projectUrl(ed.p.id, "/files/move"), { from, to });
    if (from !== ed.openPath && !(ed.openPath || "").startsWith(`${from}/`)) note(ed, `Moved to ${to}`);
  } catch (err) {
    flashError(err);
    return;
  }
  // A folder that was open stays open under its new path, and so do the open folders in it.
  const moved = [...ed.expanded].filter((p) => p === from || p.startsWith(`${from}/`));
  if (!moved.length) return;
  for (const p of moved) {
    ed.expanded.delete(p);
    ed.expanded.add(to + p.slice(from.length));
  }
  saveFolders(ed);
  paintTree(ed);
}

// LaTeX compiles `path` from now on, for everybody on this paper.
async function makeMain(ed, path) {
  try {
    const updated = await api.patch(projectUrl(ed.p.id), { main_file: path });
    Object.assign(ed.p, updated || { main_file: path });
  } catch (err) {
    flashError(err);
    return;
  }
  note(ed, `${path} is the main document now`);
  paintTree(ed);
  ed.outline.changed();
  ed.pdf.compile();
}

async function removeDialog(ed, path, count) {
  const ok = await confirmDialog({
    title: count ? `Delete the folder ${path}?` : `Delete ${path}?`,
    message: count
      ? `This deletes ${plural(count, "file")} for everybody on this paper. The history keeps the last snapshot.`
      : "This deletes the file for everybody on this paper. The history keeps the last snapshot.",
    confirmText: count ? "Delete folder" : "Delete file",
    danger: true,
  });
  if (!ok) return;
  ed.ownDelete = path;
  try {
    await api.del(fileUrl(ed, path));
    note(ed, `Deleted ${path}`);
  } catch (err) {
    flashError(err);
  }
}

function pickUpload(ed, folder) {
  ed.el.upload.dataset.folder = folder;
  ed.el.upload.click();
}

async function upload(ed, files, folder) {
  if (!files.length) return;
  const items = files.map((file) => ({ file, path: joinPath(folder, file.name) }));
  const bad = items.map((i) => pathProblem(i.path)).find(Boolean);
  if (bad) {
    flash(bad, "error");
    return;
  }
  const replacing = items.filter((i) => ed.byPath.has(i.path));
  if (replacing.length) {
    const ok = await confirmDialog({
      title: replacing.length === 1 ? `Replace ${replacing[0].path}?` : `Replace ${replacing.length} files?`,
      message: `${replacing.map((i) => i.path).join(", ")} ${replacing.length === 1 ? "exists" : "exist"} already. The upload replaces ${replacing.length === 1 ? "it" : "them"} for everybody.`,
      confirmText: "Replace",
    });
    if (!ok) return;
  }
  expand(ed, folder);
  note(ed, `Uploading ${plural(items.length, "file")}…`);
  let last = null;
  let done = 0;
  for (const item of items) {
    try {
      last = await api.put(fileUrl(ed, item.path), item.file, { contentType: item.file.type || "application/octet-stream" });
      done += 1;
    } catch (err) {
      flashError(err);
    }
  }
  if (done) note(ed, `Uploaded ${plural(done, "file")}`);
  if (items.length === 1 && last?.id) {
    ed.pendingOpen = last.id;
    queueRefresh(ed);
  }
}
