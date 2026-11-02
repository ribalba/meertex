/* The agent panel (DESIGN §20). It runs in an iframe of the editor, from the
   desktop app's meertex-agent:// scheme, and talks to the app through
   window.meertexAgent (preload.js). The page around it cannot reach into it.

   The chat itself lives in the app (agent.js). This page only draws it, so a
   reload or a move to the other place in the editor picks the chat up where
   it was. */
"use strict";

const bridge = window.meertexAgent;
const params = new URLSearchParams(location.search);
const PROJECT = params.get("project") || "";
const NAME = params.get("name") || "";
const POSITION = params.get("position") === "below" ? "below" : "column";
const DRAFT_KEY = `meertex.agent.draft.${PROJECT}`;
const CLAUDE_MODELS = [["", "Default"], ["opus", "Opus"], ["sonnet", "Sonnet"], ["haiku", "Haiku"]];

const $ = (id) => document.getElementById(id);
const el = {
  provider: $("provider"),
  model: $("model"),
  newChat: $("new"),
  toColumn: $("to-column"),
  toBelow: $("to-below"),
  close: $("close"),
  notices: $("notices"),
  log: $("log"),
  context: $("context"),
  withContext: $("with-context"),
  contextText: $("context-text"),
  input: $("input"),
  stop: $("stop"),
  send: $("send"),
};

let state = null;
const shown = new Map();
let pendingProvider = null;

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

// --- Text -------------------------------------------------------------------------

// The little Markdown Claude writes: paragraphs, lists, headings, code blocks,
// `code`, **bold** and [links](https://...). Built as nodes, never as HTML.
function markdown(text) {
  const out = document.createDocumentFragment();
  const lines = String(text).split("\n");
  const special = (line) => /^```/.test(line) || /^\s*([-*]|\d+[.)])\s/.test(line) || /^#{1,6}\s/.test(line);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const code = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) code.push(lines[i++]);
      i++;
      out.append(h("pre", {}, h("code", { text: code.join("\n") })));
    } else if (/^\s*([-*]|\d+[.)])\s/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s/.test(lines[i]) && /^\s*\d/.test(lines[i]) === ordered) {
        list.append(h("li", {}, inline(lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, ""))));
        i++;
      }
      out.append(list);
    } else if (/^#{1,6}\s/.test(line)) {
      out.append(h("p", { class: "heading" }, inline(line.replace(/^#+\s+/, ""))));
      i++;
    } else if (!line.trim()) {
      i++;
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() && !special(lines[i])) para.push(lines[i++]);
      out.append(h("p", {}, inline(para.join("\n"))));
    }
  }
  return out;
}

function inline(text) {
  const out = document.createDocumentFragment();
  const re = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    out.append(text.slice(last, m.index));
    if (m[1] !== undefined) out.append(h("code", { text: m[1] }));
    else if (m[2] !== undefined) out.append(h("strong", { text: m[2] }));
    else out.append(h("a", { href: m[4], target: "_blank", rel: "noopener", text: m[3] }));
    last = m.index + m[0].length;
  }
  out.append(text.slice(last));
  return out;
}

// --- The chat ---------------------------------------------------------------------

function atBottom() {
  const log = el.log;
  return log.scrollHeight - log.scrollTop - log.clientHeight < 48;
}

function scrollDown() {
  el.log.scrollTop = el.log.scrollHeight;
}

function itemNode(item) {
  switch (item.kind) {
    case "user":
      return h("div", { class: "msg user" },
        h("div", { class: "bubble", text: item.text }),
        item.context ? h("div", { class: "meta", text: `With ${item.context}` }) : null);
    case "assistant":
      return h("div", { class: "msg assistant" }, markdown(item.text));
    case "tool":
      return toolNode(item);
    case "note":
      return h("div", { class: "note", text: item.text });
    default:
      return h("div", { class: "error", text: item.text });
  }
}

function toolNode(item) {
  let target = null;
  if (item.target && item.path) {
    target = h("a", {
      href: "#", class: "target", text: item.target, title: "Open in the editor",
      onclick: (e) => {
        e.preventDefault();
        bridge.openFile(PROJECT, item.path, item.line || 1);
      },
    });
  } else if (item.target) {
    target = h(item.code ? "code" : "span", { class: "target", text: item.target });
  }
  const label = { running: "Running", done: "Done", error: "Failed", stopped: "Stopped", declined: "Declined" }[item.status] || "";
  return h("div", { class: `tool is-${item.status}` },
    h("span", { class: "status", title: label, "aria-label": label }),
    h("span", { class: "verb", text: item.verb }), " ", target,
    item.status === "declined" ? h("span", { class: "tool-note", text: " (declined)" }) : null,
    item.error ? h("div", { class: "tool-error", text: item.error }) : null);
}

function upsert(item) {
  const follow = atBottom();
  const node = itemNode(item);
  const old = shown.get(item.id);
  if (old) old.replaceWith(node);
  else {
    el.log.querySelector(".intro")?.remove();
    el.log.insertBefore(node, el.log.querySelector(".working"));
  }
  shown.set(item.id, node);
  const at = state.items.findIndex((x) => x.id === item.id);
  if (at >= 0) state.items[at] = item;
  else state.items.push(item);
  if (follow) scrollDown();
}

function paintLog() {
  shown.clear();
  el.log.replaceChildren();
  for (const item of state.items) {
    const node = itemNode(item);
    shown.set(item.id, node);
    el.log.append(node);
  }
  if (!state.items.length) el.log.append(intro());
  paintBusy();
  scrollDown();
}

function intro() {
  const who = provider() === "ollama" ? "The local model" : "Claude";
  const where = state.checkout?.found ? state.checkout.folder : "this paper's checkout";
  return h("div", { class: "intro" },
    h("p", { class: "intro-title", text: `${who} works on ${NAME || "this paper"} in ${where}.` }),
    h("p", { text: "Ask about the paper or tell it what to change. Its edits reach the editor and your coauthors through meertex sync. Before it runs a command or looks outside the folder, this app asks you." }));
}

function paintBusy() {
  el.log.querySelector(".working")?.remove();
  if (state.busy) {
    el.log.append(h("div", { class: "working" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Working…"));
  }
  el.stop.hidden = !state.busy;
  el.send.disabled = state.busy || Boolean(blocked());
  el.provider.disabled = state.busy;
  el.model.disabled = state.busy;
}

function paintContext() {
  const c = state.context;
  el.context.hidden = !c;
  if (!c) return;
  let text = c.path;
  if (c.from && c.to) text += c.from === c.to ? `, line ${c.from}` : `, lines ${c.from} to ${c.to}`;
  el.contextText.textContent = `Send with ${text}`;
}

// --- Choices ----------------------------------------------------------------------

function provider() {
  return state?.settings?.provider === "ollama" ? "ollama" : "claude";
}

function paintChoices() {
  el.provider.value = provider();
  const options = [];
  if (provider() === "claude") {
    for (const [value, text] of CLAUDE_MODELS) options.push(h("option", { value, text }));
    el.model.replaceChildren(...options);
    el.model.value = state.settings.claudeModel || "";
  } else {
    const models = state.ollama.models || [];
    const saved = state.settings.ollamaModel;
    if (!saved) options.push(h("option", { value: "", text: "Pick a model" }));
    for (const m of models) {
      options.push(h("option", { value: m.name, disabled: !m.tools, text: m.tools ? m.name : `${m.name} (no tools)` }));
    }
    if (saved && !models.some((m) => m.name === saved)) options.push(h("option", { value: saved, text: `${saved} (not pulled)` }));
    el.model.replaceChildren(...options);
    el.model.value = saved || "";
  }
  el.input.placeholder = provider() === "ollama"
    ? "Ask the local model about the paper, or tell it what to change"
    : "Ask Claude about the paper, or tell it what to change";
  el.toColumn.setAttribute("aria-pressed", String(POSITION === "column"));
  el.toBelow.setAttribute("aria-pressed", String(POSITION === "below"));
}

// What keeps the agent from starting, as a notice. Null when nothing does.
function blocked() {
  if (!state) return "loading";
  if (!state.checkout?.found) return "checkout";
  if (!state.claude?.found) return "claude";
  if (provider() === "ollama") {
    if (!state.ollama.running) return "ollama";
    if (!state.ollama.recent) return "ollama-old";
    if (!state.settings.ollamaModel) return "ollama-model";
  }
  return null;
}

function cmd(text) {
  return h("code", { class: "cmd", text });
}

function notice(kind, ...content) {
  return h("div", { class: `notice is-${kind}` }, ...content);
}

function paintNotices() {
  const out = [];
  const again = () => h("button", { class: "btn btn-sm", type: "button", text: "Check again", onclick: refresh });
  if (pendingProvider) {
    out.push(notice("warn",
      h("p", { text: `Switching to ${pendingProvider === "ollama" ? "Ollama" : "Claude"} starts a new chat. This one is closed.` }),
      h("div", { class: "actions" },
        h("button", { class: "btn btn-sm primary", type: "button", text: "Switch", onclick: () => switchProvider(pendingProvider) }),
        h("button", { class: "btn btn-sm", type: "button", text: "Cancel", onclick: () => { pendingProvider = null; paintChoices(); paintNotices(); } }))));
  }
  switch (blocked()) {
    case "checkout":
      out.push(notice("error",
        h("p", { text: "This paper has no checkout on this computer. The agent works in one. Make it and keep it in sync with" }),
        cmd(`meertex checkout ${NAME || "<paper>"}`), cmd("meertex sync"),
        h("div", { class: "actions" }, again())));
      break;
    case "claude":
      out.push(notice("error",
        h("p", { text: "meertex cannot find Claude Code. Install it, sign in once in a terminal, and check again. Ollama runs through Claude Code too." }),
        cmd("curl -fsSL https://claude.ai/install.sh | bash"), cmd("claude"),
        h("div", { class: "actions" }, again())));
      break;
    case "ollama":
      out.push(notice("error",
        h("p", { text: "Ollama does not answer. Start it and check again." }), cmd("ollama serve"),
        h("div", { class: "actions" }, again())));
      break;
    case "ollama-old":
      out.push(notice("error",
        h("p", { text: `Ollama ${state.ollama.version} is too old. Claude Code needs Ollama 0.14 or newer, which speaks Anthropic's API.` }),
        h("div", { class: "actions" }, again())));
      break;
    case "ollama-model":
      out.push(notice("warn", state.ollama.models.some((m) => m.tools)
        ? h("p", { text: "Pick a model at the top. It needs to call tools, and a context of at least 32k tokens." })
        : h("p", {}, "Ollama has no model that can call tools yet. Pull one, for example ", cmd("ollama pull qwen3-coder"), " and check again."),
      h("div", { class: "actions" }, again())));
      break;
    default:
      if (state?.checkout?.found && !state.checkout.syncing) {
        out.push(notice("warn",
          h("p", { text: `meertex sync is not running, so the agent's edits stay in ${state.checkout.folder} until it runs.` }),
          cmd("meertex sync"), h("div", { class: "actions" }, again())));
      } else if (state?.checkout?.held) {
        out.push(notice("info", h("p", {}, "The checkout is held. The agent's edits reach your coauthors when you run ", cmd("meertex release"), ".")));
      }
  }
  el.notices.replaceChildren(...out);
  paintBusy();
}

function paint() {
  paintChoices();
  paintNotices();
  paintContext();
  paintLog();
}

async function refresh() {
  state = await bridge.attach(PROJECT);
  if (!state) return;
  paint();
}

async function choose(choice) {
  state = await bridge.choose(PROJECT, choice);
  if (state) paint();
}

function switchProvider(next) {
  pendingProvider = null;
  const model = next === "ollama" ? state.settings.ollamaModel : state.settings.claudeModel;
  choose({ provider: next, model });
}

// --- Sending ----------------------------------------------------------------------

function autosize() {
  el.input.style.height = "auto";
  el.input.style.height = `${Math.min(el.input.scrollHeight + 2, 200)}px`;
}

function saveDraft() {
  try {
    sessionStorage.setItem(DRAFT_KEY, el.input.value);
  } catch {
    // Storage may be refused. The draft then lives as long as the page.
  }
}

function readDraft() {
  try {
    return sessionStorage.getItem(DRAFT_KEY) || "";
  } catch {
    return "";
  }
}

async function send() {
  const text = el.input.value.trim();
  if (!text || !state || state.busy || blocked()) return;
  el.input.value = "";
  saveDraft();
  autosize();
  const sent = await bridge.send(PROJECT, text, el.withContext.checked && Boolean(state.context));
  // Refused before anything was said, because a turn was still running.
  if (sent === false && state.busy && !el.input.value) {
    el.input.value = text;
    saveDraft();
    autosize();
  }
}

// --- Wiring -----------------------------------------------------------------------

el.provider.addEventListener("change", () => {
  const next = el.provider.value;
  if (next === provider()) return;
  if (state.items.length) {
    pendingProvider = next;
    paintNotices();
    return;
  }
  switchProvider(next);
});
el.model.addEventListener("change", () => choose({ provider: provider(), model: el.model.value }));
el.newChat.addEventListener("click", () => bridge.reset(PROJECT));
el.toColumn.addEventListener("click", () => bridge.layout(PROJECT, "column"));
el.toBelow.addEventListener("click", () => bridge.layout(PROJECT, "below"));
el.close.addEventListener("click", () => bridge.layout(PROJECT, "close"));
el.stop.addEventListener("click", () => bridge.stop(PROJECT));
el.send.addEventListener("click", send);
el.input.addEventListener("input", () => {
  saveDraft();
  autosize();
});
el.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  } else if (e.key === "Escape" && state?.busy) {
    e.preventDefault();
    bridge.stop(PROJECT);
  }
});

bridge.onEvent((ev) => {
  if (!state) return;
  if (ev.type === "item") upsert(ev.item);
  else if (ev.type === "busy") {
    state.busy = ev.busy;
    paintBusy();
    if (!ev.busy && atBottom()) scrollDown();
  } else if (ev.type === "context") {
    state.context = ev.context;
    paintContext();
  } else if (ev.type === "reset") {
    state.items = [];
    state.busy = false;
    paintLog();
  }
});

el.input.value = readDraft();
autosize();
refresh();
