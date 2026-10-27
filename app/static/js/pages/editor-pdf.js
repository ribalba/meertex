// The editor's PDF pane (docs/DESIGN.md §14, In the editor): the compiled PDF
// of the live document, compiling on request and two seconds after the last
// edit, the problems of the last compile, and SyncTeX in both directions.
//
// The server compiles and caches by content, so every open editor may ask
// after every edit. Only one request runs here at a time. Edits meanwhile ask
// once more when it is done. So does a request right after typing, because the
// typing goes over the socket and the request over HTTP, and the server may
// compile before the last keys are in. Asking again costs little, because the same
// content comes back from the cache.

import { api, projectUrl } from "../api.js";
import { h, clear, plural } from "../dom.js";
import { icon } from "../icons.js";
import { createPdfView } from "../pdfview.js";
import { dropdown, spinner } from "../ui.js";

const AUTO_KEY = "meertex.editor.autocompile";
const AUTO_DELAY_MS = 2000;
const POLL_MS = 800;
// A local edit this recent may not have reached the server when a compile starts.
const IN_FLIGHT_MS = 1000;
const FINISHED = new Set(["success", "errors", "failure", "timeout"]);
const ZOOM_PRESETS = [0.5, 0.75, 1, 1.25, 1.5, 2];

function readAuto() {
  try {
    return localStorage.getItem(AUTO_KEY) !== "off";
  } catch {
    return true;
  }
}

function seconds(ms) {
  return ms >= 10000 ? `${Math.round(ms / 1000)} s` : `${(ms / 1000).toFixed(1)} s`;
}

// `project` is the project. `openSource({path, line, column})` shows a place in
// the text, `cursor()` says where the cursor is ({path, line, column} or null),
// `note(text)` shows a short note. `onHide()` goes back to the text on a phone,
// where the PDF takes the text's place. `onProblems(problems)` gets every
// finished compile's problems, for the editor to mark their lines. The
// reference check's errors and warnings (docs/DESIGN.md §16) come with them,
// read when the pane opens and after every compile, marked "References".
export function pdfPane({ project, openSource, cursor, note, onHide, onProblems = null }) {
  const url = (rest) => projectUrl(project.id, rest);
  let auto = readAuto();
  let shown = null; // the compile whose PDF is in the view
  let last = null; // the newest finished compile
  let running = null;
  let again = false;
  let timer = 0;
  let lastLocalEdit = 0;
  let dead = false;
  let problemsOpen = null; // null until the person opens or closes the list
  let refProblems = []; // the reference check's errors and warnings
  let refAsked = 0;

  const status = h("span", { class: "pdf-status is-idle", role: "status" }, h("span", { class: "dot" }), h("span", { class: "pdf-status-text", text: "Not compiled yet" }));
  const recompile = h("button", { type: "button", class: "btn btn-sm pdf-recompile", title: "Compile the paper now (Ctrl+Enter)" }, "Recompile");
  recompile.addEventListener("click", () => compile());
  const autoMenu = dropdown({
    summary: icon("triangle-down"),
    summaryClass: "btn btn-sm pdf-recompile-more",
    label: "Compile options",
    items: () => [
      { text: "Compile while you type", checked: auto, onClick: () => setAuto(!auto) },
      last?.log_url ? "divider" : null,
      last?.log_url ? { text: "Open the log", icon: "file-text", href: last.log_url } : null,
    ],
  });
  // The log opens in a tab of its own, past the app's own link handling.
  autoMenu.querySelector(".dropdown-menu").addEventListener("click", (e) => {
    const link = e.target.closest("a[href]");
    if (link) link.target = "_blank";
  });

  const showButton = h("button", {
    type: "button", class: "btn-octicon", "aria-label": "Show the cursor's place in the PDF",
    title: "Show the cursor's place in the PDF (Ctrl+Shift+Enter). A double-click in the PDF shows its place in the text.",
  }, icon("arrow-right"));
  showButton.addEventListener("click", () => showCursor());
  const zoomOut = h("button", { type: "button", class: "btn-octicon pdf-zoom-step", "aria-label": "Zoom out", title: "Zoom out", text: "−" });
  const zoomIn = h("button", { type: "button", class: "btn-octicon pdf-zoom-step", "aria-label": "Zoom in", title: "Zoom in", text: "+" });
  const zoomLabel = h("span", { text: "100%" });
  const zoomMenu = dropdown({
    summary: [zoomLabel, icon("triangle-down")],
    summaryClass: "btn btn-sm pdf-zoom",
    label: "Zoom",
    align: "right",
    items: () => [
      { text: "Fit the width", checked: view.mode === "width", onClick: () => view.setZoom("width") },
      "divider",
      ...ZOOM_PRESETS.map((z) => ({ text: `${Math.round(z * 100)}%`, checked: view.mode === "fixed" && Math.abs(view.zoom - z) < 0.001, onClick: () => view.setZoom(z) })),
    ],
  });
  zoomOut.addEventListener("click", () => view.zoomOut());
  zoomIn.addEventListener("click", () => view.zoomIn());
  const pageCount = h("span", { class: "pdf-pages", hidden: true });
  const download = h("a", { class: "btn-octicon", "aria-label": "Download the PDF", title: "Download the PDF", hidden: true }, icon("download"));

  const toolbar = h("div", { class: "pdf-toolbar" },
    h("div", { class: "pdf-split" }, recompile, autoMenu),
    status,
    h("span", { class: "flex-1" }),
    showButton, zoomOut, zoomMenu, zoomIn, pageCount, download,
    h("button", { type: "button", class: "btn-octicon pdf-hide", "aria-label": "Back to the text", onclick: () => onHide?.() }, icon("x")));
  const viewHost = h("div", { class: "pdf-view" });
  const message = h("div", { class: "pdf-message" }, h("div", { class: "loading" }, spinner(true)));
  const stale = h("div", { class: "pdf-stale", hidden: true }, icon("alert", { size: 14 }),
    h("span", { text: "The last compile made no PDF. This one is from before it." }));
  const problems = h("div", { class: "pdf-problems", hidden: true });
  const el = h("section", { class: "pdf-pane", "aria-label": "PDF" }, toolbar, stale, h("div", { class: "pdf-body" }, viewHost, message), problems);

  const view = createPdfView(viewHost, {
    onDoubleClick: (at) => findSource(at),
    onChange: ({ page, pages, zoom }) => {
      zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
      pageCount.hidden = !pages;
      pageCount.textContent = `${page} / ${pages}`;
      pageCount.title = `Page ${page} of ${pages}`;
    },
  });

  function setStatus(kind, text) {
    status.className = `pdf-status is-${kind}`;
    status.title = text;
    status.lastChild.textContent = text;
  }

  function setAuto(on) {
    auto = on;
    try {
      localStorage.setItem(AUTO_KEY, on ? "on" : "off");
    } catch {
      // The choice holds for this page.
    }
    note(on ? "Compiles two seconds after the last edit" : "Compiles only on Recompile and Ctrl+Enter");
  }

  function showMessage(node) {
    clear(message).append(node);
    message.hidden = false;
  }

  function describe(c) {
    const errors = (c.problems || []).filter((p) => p.level === "error").length;
    if (c.status === "success") return ["ok", `Compiled in ${seconds(c.duration_ms || 0)}`];
    if (c.status === "errors") return ["errors", plural(errors || 1, "error")];
    if (c.status === "timeout") return ["failed", "Timed out, the compile took too long"];
    return ["failed", errors ? `Failed with ${plural(errors, "error")}` : "Failed, no PDF"];
  }

  async function show(c) {
    last = c;
    onProblems?.(forLines(c));
    const [kind, text] = describe(c);
    setStatus(kind, text);
    paintProblems(c);
    stale.hidden = Boolean(c.pdf_url) || !shown;
    if (c.pdf_url) {
      download.href = c.pdf_url;
      download.download = `${project.name}.pdf`;
      download.hidden = false;
      if (shown?.id !== c.id) {
        try {
          await view.load(c.pdf_url);
          shown = c;
          message.hidden = true;
        } catch (err) {
          // A PDF (or PDF.js) that could not be fetched, because the server was
          // out of reach for a moment, is loaded with the next compile.
          const unreachable = err instanceof TypeError || /^(UnknownErrorException|ResponseException|MissingPDFException)$/.test(err?.name || "");
          if (unreachable) console.warn("The PDF could not be fetched", err);
          else console.error(err);
          if (!shown) showMessage(h("p", { class: "color-muted", text: "The PDF could not be shown." }));
        }
      }
    } else if (!shown) {
      showMessage(h("div", { class: "blankslate" },
        icon("alert", { size: 24 }),
        h("h3", { text: c.status === "timeout" ? "The compile took too long" : "LaTeX made no PDF" }),
        h("p", { text: (c.problems || []).length ? "The problems below say why." : "The log says why." }),
        c.log_url ? h("a", { class: "btn", href: c.log_url, target: "_blank", rel: "noopener" }, "Open the log") : null));
    }
  }

  function paintProblems(c) {
    const list = [...(c?.problems || []), ...refProblems];
    clear(problems);
    problems.hidden = !list.length;
    if (!list.length) return;
    const errors = list.filter((p) => p.level === "error").length;
    const warnings = list.length - errors;
    // It opens by itself for LaTeX's errors, not for the reference check's.
    const open = problemsOpen ?? (c?.problems || []).some((p) => p.level === "error");
    const head = h("button", { type: "button", class: "pdf-problems-head", "aria-expanded": String(open) },
      icon(open ? "chevron-down" : "chevron-right", { size: 12 }),
      h("span", { class: "flex-1" },
        errors ? h("strong", { class: "color-danger", text: plural(errors, "error") }) : null,
        errors && warnings ? " · " : "",
        warnings ? h("span", { text: plural(warnings, "warning") }) : null),
      c?.log_url ? h("a", { class: "text-small", href: c.log_url, target: "_blank", rel: "noopener", text: "Log", onclick: (e) => e.stopPropagation() }) : null);
    head.addEventListener("click", () => {
      problemsOpen = !open;
      paintProblems(c);
    });
    problems.append(head);
    if (!open) return;
    const order = { error: 0, warning: 1, badbox: 2 };
    const items = [...list].sort((a, b) => (order[a.level] ?? 3) - (order[b.level] ?? 3));
    problems.append(h("ul", { class: "pdf-problem-list" }, items.map((p) => {
      const where = p.path ? `${p.path}${p.line ? `:${p.line}` : ""}` : "";
      return h("li", { class: `pdf-problem is-${p.level}` },
        icon(p.level === "error" ? "x-circle" : "alert", { size: 14 }),
        where
          ? h("button", { type: "button", class: "btn-link pdf-problem-where", text: where, onclick: () => openSource({ path: p.path, line: p.line || 1, column: 0 }) })
          : null,
        p.references ? h("span", { class: "Label rf-problem-tag", title: "From the reference check", text: "References" }) : null,
        h("span", { class: "pdf-problem-message", text: p.message }));
    })));
  }

  // The problems for the lines in the text. The reference check's carry
  // their own source, which the hover shows under the message.
  function forLines(c) {
    return [...(c?.problems || []), ...refProblems.map((p) => ({ ...p, source: "References" }))];
  }

  // The reference check's errors and warnings, in a compile's shape. A failing
  // request keeps the last ones, and the editor works on without them.
  async function loadReferenceProblems() {
    const mine = ++refAsked;
    try {
      const list = await api.get(url("/references/problems"));
      if (dead || mine !== refAsked || !Array.isArray(list)) return;
      refProblems = list
        .filter((p) => p && (p.level === "error" || p.level === "warning") && typeof p.message === "string")
        .map((p) => ({ level: p.level, path: p.path || null, line: p.line || null, message: p.message, references: true }));
      paintProblems(last);
      onProblems?.(forLines(last));
    } catch (err) {
      if (!dead) console.warn("The reference check's problems could not be read", err);
    }
  }

  async function finish(c) {
    while (!FINISHED.has(c.status) && !dead) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      c = await api.get(url(`/compiles/${encodeURIComponent(c.id)}`));
    }
    return c;
  }

  // Compile the live document now. A compile that runs already gets one more after it.
  function compile() {
    clearTimeout(timer);
    if (running) {
      again = true;
      return running;
    }
    setStatus("running", "Compiling…");
    recompile.disabled = true;
    if (Date.now() - lastLocalEdit < IN_FLIGHT_MS) again = true;
    running = (async () => {
      try {
        const c = await finish(await api.post(url("/compile"), { ref: "live" }));
        if (!dead) {
          loadReferenceProblems();
          await show(c);
        }
      } catch (err) {
        if (dead) return;
        if (err.status === 503) {
          setStatus("off", "Compiling is not set up on this server");
          if (!shown) showMessage(h("div", { class: "blankslate" }, icon("info", { size: 24 }), h("h3", { text: "No compiling here" }),
            h("p", { text: "This server has no compile service. Ask whoever runs it to set COMPILE_URL." })));
        } else {
          setStatus("failed", err.message || "The compile did not finish");
        }
      } finally {
        running = null;
        recompile.disabled = false;
        if (again && !dead) {
          again = false;
          // Not before the last local edit is a second old, which also keeps
          // this from asking again and again.
          const wait = lastLocalEdit + IN_FLIGHT_MS - Date.now();
          if (wait > 0) timer = setTimeout(() => compile(), wait);
          else compile();
        }
      }
    })();
    return running;
  }

  // The live document changed, here (`local`) or elsewhere. With auto compile
  // on, compile when it rests.
  function edited(local = false) {
    if (local) lastLocalEdit = Date.now();
    if (!auto || dead) return;
    clearTimeout(timer);
    timer = setTimeout(() => compile(), AUTO_DELAY_MS);
  }

  async function synctex(rest, body) {
    if (!shown) return null;
    try {
      return await api.post(url(`/compiles/${encodeURIComponent(shown.id)}/synctex/${rest}`), body);
    } catch (err) {
      if (err.status === 410) note("This PDF is too old to find places in. Recompile and try again.");
      else note(err.message);
      return null;
    }
  }

  async function findSource(at) {
    const place = await synctex("edit", { page: at.page, x: at.x, y: at.y });
    if (place?.path) {
      openSource({
        path: place.path, line: place.line || 1, column: place.column || 0,
        spot: { word: at.word || "", before: at.before || "", after: at.after || "" },
      });
    }
    else if (place) note("SyncTeX found no source for that place.");
  }

  async function showCursor() {
    const at = cursor();
    if (!at) {
      note("Open a .tex file and put the cursor somewhere first.");
      return;
    }
    if (!shown) {
      note("There is no PDF yet. Recompile first.");
      return;
    }
    const found = await synctex("view", at);
    if (!found) return;
    if (found.boxes?.length) view.showBoxes(found.boxes);
    else note("Nothing in the PDF comes from this line.");
  }

  // The newest PDF there is, then a compile, which the server answers from its
  // cache when nothing changed. The reference check's problems come along,
  // also when there is no compile service.
  async function start() {
    loadReferenceProblems();
    try {
      // null when the paper was never compiled.
      const c = await api.get(url("/compiles/latest"), { query: { ref: "live" } });
      if (!dead && c && FINISHED.has(c.status)) await show(c);
    } catch (err) {
      if (err.status !== 404 && err.status !== 503) console.error(err);
    }
    if (!dead) compile();
  }
  start();

  return {
    el,
    compile,
    edited,
    showCursor,
    destroy() {
      dead = true;
      clearTimeout(timer);
      view.destroy();
    },
  };
}
