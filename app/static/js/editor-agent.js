// The agent panel of the desktop app (docs/DESIGN.md §20). There the editor
// can show a chat with the Claude Code or the Ollama model of the person's own
// computer, as a fourth column next to the PDF or under the text. The agent
// works in the paper's checkout, and its edits come back over the socket like
// anyone's.
//
// The panel is the app's own page (meertex-agent://panel) in an iframe, so this
// page cannot script it, and the prompts are typed there. This side only puts
// it in its place, tells the app which file is open and which lines are
// selected, and opens the files the agent names. In a browser there is no
// panel, and the Agent tab says how to get one.

import { h } from "./dom.js";
import { icon } from "./icons.js";

const POSITION_KEY = "meertex.editor.agent";
const LAST_KEY = "meertex.editor.agentlast";
const WIDTH_KEY = "meertex.editor.agentwidth";
const HEIGHT_KEY = "meertex.editor.agentheight";
const WIDTH = 380;
const HEIGHT = 300;

// The desktop app's bridge (electron/preload.js), or null in a browser.
export function desktopAgent() {
  const agent = window.meertexDesktop?.agent;
  return agent && typeof agent.setContext === "function" ? agent : null;
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

// `view()` and `path()` give the open text and file, `open({path, line})`
// opens one. Null outside the desktop app and for readers, since the agent
// edits the paper.
export function editorAgent({ project, canEdit, cm, view, path, open }) {
  const bridge = desktopAgent();
  if (!bridge || !canEdit) return null;

  let position = ["column", "below"].includes(stored(POSITION_KEY, "off")) ? stored(POSITION_KEY, "off") : "off";
  let shell = null;
  let main = null;
  let timer = null;

  const frame = h("iframe", { class: "editor-agent-frame", title: "Agent", referrerpolicy: "no-referrer" });
  const box = h("section", { class: "editor-agent", "aria-label": "Agent" }, frame);
  const divider = h("div", { class: "editor-agent-divider", role: "separator", tabindex: "0" });
  const button = h("button", {
    type: "button", class: "btn btn-sm editor-agent-btn", "aria-label": "Agent", "aria-pressed": "false",
    title: "Show or hide the agent of this computer",
  }, icon("sparkle"), h("span", { class: "hide-sm hide-narrow", text: "Agent" }));
  button.addEventListener("click", () => {
    if (position !== "off") place("off");
    else place(stored(LAST_KEY, "column") === "below" ? "below" : "column");
  });

  function panelSrc() {
    const q = new URLSearchParams({ project: project.id, name: project.name, position });
    return `${bridge.panelOrigin}/?${q}`;
  }

  // An iframe loads again whenever it moves in the page. The chat lives in
  // the app, so the panel picks it up where it was.
  function place(next) {
    position = next;
    store(POSITION_KEY, next);
    if (next !== "off") store(LAST_KEY, next);
    button.setAttribute("aria-pressed", String(next !== "off"));
    if (!shell) return;
    shell.classList.toggle("agent-column", next === "column");
    shell.classList.toggle("agent-below", next === "below");
    box.remove();
    divider.remove();
    if (next === "off") return;
    const column = next === "column";
    divider.setAttribute("aria-orientation", column ? "vertical" : "horizontal");
    divider.setAttribute("aria-label", column ? "Width of the agent" : "Height of the agent");
    divider.title = "Drag to change the size. A double-click gives the agent its usual size.";
    frame.src = panelSrc();
    box.style.flexBasis = column ? `${Number(stored(WIDTH_KEY, "")) || WIDTH}px` : "";
    box.style.height = column ? "" : `${Number(stored(HEIGHT_KEY, "")) || HEIGHT}px`;
    if (column) shell.append(divider, box);
    else main.append(divider, box);
    report();
  }

  // Width in the column, height under the text. Both leave the text room.
  function resize(px) {
    if (position === "column") {
      const width = Math.round(Math.max(260, Math.min(px, shell.clientWidth / 2)));
      box.style.flexBasis = `${width}px`;
      store(WIDTH_KEY, String(width));
    } else if (position === "below") {
      const height = Math.round(Math.max(140, Math.min(px, main.clientHeight - 180)));
      box.style.height = `${height}px`;
      store(HEIGHT_KEY, String(height));
    }
  }

  function size() {
    return position === "column" ? box.offsetWidth : box.offsetHeight;
  }

  divider.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    divider.setPointerCapture(e.pointerId);
    shell.classList.add("is-resizing", position === "below" ? "is-resizing-rows" : "is-resizing-cols");
    divider.classList.add("is-dragging");
    const move = (m) => resize(position === "column"
      ? shell.getBoundingClientRect().right - m.clientX
      : main.getBoundingClientRect().bottom - m.clientY);
    const up = () => {
      shell.classList.remove("is-resizing", "is-resizing-rows", "is-resizing-cols");
      divider.classList.remove("is-dragging");
      divider.removeEventListener("pointermove", move);
      divider.removeEventListener("pointerup", up);
      divider.removeEventListener("pointercancel", up);
    };
    divider.addEventListener("pointermove", move);
    divider.addEventListener("pointerup", up);
    divider.addEventListener("pointercancel", up);
  });
  divider.addEventListener("dblclick", () => resize(position === "column" ? WIDTH : HEIGHT));
  divider.addEventListener("keydown", (e) => {
    const grow = position === "column" ? "ArrowLeft" : "ArrowUp";
    const shrink = position === "column" ? "ArrowRight" : "ArrowDown";
    if (e.key !== grow && e.key !== shrink) return;
    e.preventDefault();
    resize(size() + (e.key === grow ? 32 : -32));
  });

  // The open file, the cursor's line and the selected lines, but no text. The
  // agent reads the file in the checkout, which sync keeps the same.
  function report() {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const file = path();
      const v = view();
      if (!file) {
        bridge.setContext(project.id, null);
        return;
      }
      const context = { path: file };
      if (v) {
        const { doc, selection } = v.state;
        const sel = selection.main;
        context.line = doc.lineAt(sel.head).number;
        if (!sel.empty) {
          context.from = doc.lineAt(sel.from).number;
          // A selection that ends at the start of a line does not take that line.
          const end = doc.lineAt(sel.to);
          context.to = end.from === sel.to && end.number > context.from ? end.number - 1 : end.number;
        }
      }
      bridge.setContext(project.id, context);
    }, 250);
  }

  const offs = [
    bridge.onOpenFile((projectId, file, line) => {
      if (projectId === project.id) open({ path: file, line });
    }),
    bridge.onLayout((projectId, action) => {
      if (projectId === project.id) place(action === "close" ? "off" : action);
    }),
  ];

  return {
    button,
    mount(shellEl, mainEl) {
      shell = shellEl;
      main = mainEl;
      place(position);
    },
    extension() {
      return cm().EditorView.updateListener.of((u) => {
        if (u.selectionSet || u.docChanged) report();
      });
    },
    opened: report,
    destroy() {
      clearTimeout(timer);
      offs.forEach((off) => off?.());
      bridge.setContext(project.id, null);
    },
  };
}
