/* Local agents in the editor (docs/DESIGN.md §20).
 *
 * The editor can show an agent panel as a fourth column or under the text.
 * The panel is a page of this app, not of the server. It is served from the
 * meertex-agent:// scheme below and sits in an iframe of the editor. The
 * server's page cannot script it, because the two have different origins.
 * The page only gets a small bridge from preload.js, for the open file and
 * selection, the status on the Agent tab, and the panel asking to be moved or
 * closed. Prompts are typed in the panel only, and every tool call Claude Code
 * asks about is a native dialog of this app. So a page of the server can
 * neither start the agent nor click its approvals.
 *
 * The agent is the `claude` command the person installed and signed in to
 * themselves. It runs in the paper's checkout (`meertex checkout`), and the
 * meertex sync service carries its edits to the live document, where they show
 * up in the editor like a coauthor's. With Ollama the same command talks to a
 * local model through Ollama's Anthropic API.
 *
 * The protocol is Claude Code's stream-json over stdin and stdout, with
 * permission prompts as control requests (--permission-prompt-tool=stdio).
 * Anthropic's agent SDKs use the same transport. The SDK itself is not
 * bundled, because its license does not allow that in an AGPL app.
 */
const { BrowserWindow, app, dialog, ipcMain, protocol } = require("electron");
const { execFile, spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const SCHEME = "meertex-agent";
const PANEL_ORIGIN = `${SCHEME}://panel`;
const PANEL_FILES = {
  "/": ["index.html", "text/html"],
  "/index.html": ["index.html", "text/html"],
  "/panel.js": ["panel.js", "text/javascript"],
  "/panel.css": ["panel.css", "text/css"],
};
const PROVIDERS = ["claude", "ollama"];
// Claude Code's own aliases. The empty one leaves the choice to its settings.
const CLAUDE_MODELS = ["", "opus", "sonnet", "haiku"];
// Ollama answers Anthropic's Messages API from 0.14 on.
const OLLAMA_MIN = [0, 14, 0];
const DEFAULTS = { provider: "claude", claudeModel: "", ollamaModel: "" };
// What a running Claude Code session puts into the environment of what it
// starts. An app started from such a session, `make desktop` say, would hand
// it on, and the agent would take itself for part of that session.
const SESSION_ENV = /^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|CLAUDE_AGENT_SDK_VERSION|CLAUDE_CODE_(SESSION_ID|SESSION_ATTENDED|CHILD_SESSION|ENTRYPOINT|EXECPATH|MESSAGING_\w+|SSE_PORT|STARTUP_\w+|EMIT_STARTUP_TIMING|ENABLE_SDK_FILE_CHECKPOINTING|QUESTION_PREVIEW_FORMAT|ENABLE_TASKS))$/;
// A chat keeps at most this many entries. Older ones scroll away.
const MAX_ITEMS = 400;
const STDERR_LINES = 20;

let appOrigin = null;
let ollamaBase = ollamaUrl(process.env.OLLAMA_HOST);
const sessions = new Map();
const contexts = new Map();

// Before the app is ready. A standard, secure scheme gets a real origin
// (meertex-agent://panel), so the server's page cannot reach into the frame.
function registerScheme() {
  protocol.registerSchemesAsPrivileged([{ scheme: SCHEME, privileges: { standard: true, secure: true } }]);
}

function setup(ses, origin) {
  appOrigin = origin;
  ses.protocol.handle(SCHEME, servePanel);

  // From the server's page (the editor and the Agent tab).
  ipcMain.handle("agent:status", (e, projectId) => (fromPage(e) && isId(projectId) ? status(projectId) : null));
  ipcMain.on("agent:context", (e, projectId, context) => {
    if (fromPage(e) && isId(projectId)) setContext(projectId, cleanContext(context));
  });

  // From the panel.
  ipcMain.handle("agent:attach", (e, projectId) => panelCall(e, projectId, (s) => s.attach(e)));
  ipcMain.handle("agent:send", (e, projectId, text, withContext) =>
    panelCall(e, projectId, (s) => s.send(e, String(text || "").slice(0, 100000), Boolean(withContext))));
  ipcMain.handle("agent:stop", (e, projectId) => panelCall(e, projectId, (s) => s.stop()));
  ipcMain.handle("agent:reset", (e, projectId) => panelCall(e, projectId, (s) => s.reset()));
  ipcMain.handle("agent:settings", (e, projectId, choice) => panelCall(e, projectId, (s) => s.choose(e, choice)));
  ipcMain.on("agent:open", (e, projectId, file, line) => {
    if (!fromPanel(e) || !isId(projectId)) return;
    const rel = sessions.get(projectId)?.relative(String(file || ""));
    if (rel) e.sender.send("agent:open-file", projectId, rel, Number.isInteger(line) && line > 0 ? line : 1);
  });
  ipcMain.on("agent:layout", (e, projectId, action) => {
    if (fromPanel(e) && isId(projectId) && ["column", "below", "close"].includes(action)) {
      e.sender.send("agent:layout", projectId, action);
    }
  });

  app.on("will-quit", () => sessions.forEach((s) => s.kill()));
}

function servePanel(request) {
  const url = new URL(request.url);
  const file = url.host === "panel" && request.method === "GET" ? PANEL_FILES[url.pathname] : null;
  if (!file) return new Response("Not found", { status: 404 });
  return new Response(fs.readFileSync(path.join(__dirname, "panel", file[0])), {
    headers: {
      "Content-Type": `${file[1]}; charset=utf-8`,
      // Only the editor of this app's server may show the panel.
      "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        + `base-uri 'none'; form-action 'none'; frame-ancestors ${appOrigin}`,
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "no-store",
    },
  });
}

// The frame that sent an IPC message, by origin. preload.js hands the agent
// bridge to the panel only and the page bridge to the server's pages only, and
// these checks hold the same line here.
function fromPage(e) {
  return e.senderFrame?.origin === appOrigin;
}

function fromPanel(e) {
  return e.senderFrame?.origin === PANEL_ORIGIN;
}

function isId(value) {
  return typeof value === "string" && /^[0-9a-f-]{32,36}$/i.test(value);
}

function panelCall(e, projectId, fn) {
  if (!fromPanel(e) || !isId(projectId)) return null;
  let session = sessions.get(projectId);
  if (!session) {
    session = new Session(projectId);
    sessions.set(projectId, session);
  }
  return fn(session);
}

// --- Settings -------------------------------------------------------------------

function settingsFile() {
  return path.join(app.getPath("userData"), "agent.json");
}

function readSettings() {
  const saved = readJson(settingsFile(), {});
  const s = { ...DEFAULTS, ...(saved && typeof saved === "object" ? saved : {}) };
  if (!PROVIDERS.includes(s.provider)) s.provider = DEFAULTS.provider;
  if (!CLAUDE_MODELS.includes(s.claudeModel)) s.claudeModel = DEFAULTS.claudeModel;
  if (!isModelName(s.ollamaModel)) s.ollamaModel = DEFAULTS.ollamaModel;
  return s;
}

function saveSettings(s) {
  try {
    fs.writeFileSync(settingsFile(), JSON.stringify(s, null, 2));
  } catch (err) {
    console.warn(`agent: could not save the settings. ${err.message}`);
  }
}

function isModelName(value) {
  return typeof value === "string" && value.length <= 200 && /^[\w.:/-]*$/.test(value);
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

// --- The checkout ---------------------------------------------------------------

// The CLI keeps its files in MEERTEX_HOME, ~/.meertex by default, and lists
// every checkout of this computer in checkouts.json (DESIGN §10).
function meertexHome() {
  return process.env.MEERTEX_HOME || path.join(os.homedir(), ".meertex");
}

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

function findCheckout(projectId) {
  const list = readJson(path.join(meertexHome(), "checkouts.json"), []);
  if (!Array.isArray(list)) return null;
  for (const c of list) {
    if (!c || c.project_id !== projectId || typeof c.dir !== "string") continue;
    if (originOf(c.server) !== appOrigin) continue;
    const meta = readJson(path.join(c.dir, ".meertex", "project.json"), null);
    if (meta?.project_id !== projectId) continue;
    return { dir: c.dir, name: c.name || meta.name || path.basename(c.dir) };
  }
  return null;
}

// Whether a sync client holds the checkout, from the status.json it writes.
function syncState(dir) {
  const status = readJson(path.join(dir, ".meertex", "status.json"), {}) || {};
  const running = alive(status.pid);
  return {
    syncing: running,
    connected: running && Boolean(status.connected),
    held: fs.existsSync(path.join(dir, ".meertex", "hold")),
  };
}

function alive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === "EPERM";
  }
}

// ~/papers/x instead of /home/someone/papers/x. The server's page gets this
// form only, so it does not learn the account name.
function tilde(dir) {
  const home = os.homedir();
  return dir === home || dir.startsWith(home + path.sep) ? `~${dir.slice(home.length)}` : dir;
}

// --- Programs ---------------------------------------------------------------------

// An app started from the desktop gets a short PATH on macOS and on some Linux
// desktops. Claude Code runs latexmk and the like, so it gets the PATH of a
// login shell, as a terminal would.
let envPromise = null;

function shellEnv() {
  envPromise ??= loginShellPath().then((p) => {
    const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !SESSION_ENV.test(k)));
    const dirs = (p || env.PATH || "").split(path.delimiter).filter(Boolean);
    for (const extra of [path.join(os.homedir(), ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin"]) {
      if (process.platform !== "win32" && !dirs.includes(extra)) dirs.push(extra);
    }
    env.PATH = dirs.join(path.delimiter);
    return env;
  });
  return envPromise;
}

function loginShellPath() {
  if (process.platform === "win32") return Promise.resolve(null);
  const mark = "__MEERTEX_PATH__";
  return new Promise((resolve) => {
    execFile(process.env.SHELL || "/bin/sh", ["-ilc", `printf '${mark}%s${mark}' "$PATH"`],
      { timeout: 5000, encoding: "utf8" }, (_err, stdout) => {
        const m = new RegExp(`${mark}(.*?)${mark}`).exec(stdout || "");
        resolve(m ? m[1] : null);
      });
  });
}

// MEERTEX_CLAUDE names the command, or it is looked up on the PATH. On
// Windows only the native claude.exe works, because a .cmd needs a shell.
function findClaude(env) {
  if (process.env.MEERTEX_CLAUDE) return process.env.MEERTEX_CLAUDE;
  const names = process.platform === "win32" ? ["claude.exe"] : ["claude"];
  const dirs = [...(env.PATH || "").split(path.delimiter), path.join(os.homedir(), ".local", "bin"), path.join(os.homedir(), ".claude", "local")];
  for (const dir of dirs.filter(Boolean)) {
    for (const name of names) {
      const file = path.join(dir, name);
      try {
        if (fs.statSync(file).isFile()) return file;
      } catch {
        // Not here.
      }
    }
  }
  return null;
}

function claudeVersion(bin, env) {
  return new Promise((resolve) => {
    execFile(bin, ["--version"], { env, timeout: 15000, encoding: "utf8", windowsHide: true }, (err, stdout) => {
      resolve(err ? null : (String(stdout).trim().split(/\s+/)[0] || null));
    });
  });
}

// OLLAMA_HOST is what `ollama serve` listens on: 127.0.0.1:11434, 0.0.0.0 or
// http://box:11434. Without it Ollama is on 127.0.0.1:11434.
function ollamaUrl(host) {
  let value = String(host || "").trim();
  if (!value) return "http://127.0.0.1:11434";
  if (!/^https?:\/\//.test(value)) value = `http://${value}`;
  try {
    const url = new URL(value);
    if (url.hostname === "0.0.0.0") url.hostname = "127.0.0.1";
    if (!url.port && url.protocol === "http:") url.port = "11434";
    return url.origin;
  } catch {
    return "http://127.0.0.1:11434";
  }
}

async function ollamaGet(route) {
  try {
    const res = await fetch(`${ollamaBase}${route}`, { signal: AbortSignal.timeout(2000) });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

// Ollama's models, and whether they can call tools. Claude Code is nothing
// without tools. An Ollama too old to say counts as yes.
async function ollamaState() {
  const [tags, version] = await Promise.all([ollamaGet("/api/tags"), ollamaGet("/api/version")]);
  if (!tags) return { running: false, version: null, recent: false, models: [] };
  const models = (Array.isArray(tags.models) ? tags.models : [])
    .filter((m) => isModelName(m?.name))
    .map((m) => ({ name: m.name, tools: !Array.isArray(m.capabilities) || m.capabilities.includes("tools") }));
  const v = typeof version?.version === "string" ? version.version : null;
  return { running: true, version: v, recent: !v || atLeast(v, OLLAMA_MIN), models };
}

function atLeast(version, min) {
  const parts = version.split(/[.-]/).map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < min.length; i++) {
    if ((parts[i] || 0) !== min[i]) return (parts[i] || 0) > min[i];
  }
  return true;
}

// What the Agent tab and the panel show: Claude Code, Ollama, and this
// paper's checkout with its sync.
async function status(projectId) {
  const env = await shellEnv();
  const bin = findClaude(env);
  const [version, ollama] = await Promise.all([bin ? claudeVersion(bin, env) : null, ollamaState()]);
  const checkout = findCheckout(projectId);
  return {
    claude: { found: Boolean(bin), version },
    ollama,
    checkout: checkout ? { found: true, folder: tilde(checkout.dir), ...syncState(checkout.dir) } : { found: false },
    settings: readSettings(),
  };
}

// --- The editor's context -----------------------------------------------------------

// The file the editor has open and the selected lines. Only these numbers
// and the path come from the page, never text.
function cleanContext(c) {
  if (!c || typeof c !== "object" || typeof c.path !== "string" || !c.path || c.path.length > 1024) return null;
  const n = (v) => (Number.isInteger(v) && v > 0 && v < 1e7 ? v : null);
  const from = n(c.from);
  const to = n(c.to);
  return { path: c.path, line: n(c.line), from: from && to && to >= from ? from : null, to: from && to && to >= from ? to : null };
}

function setContext(projectId, context) {
  contexts.set(projectId, context);
  sessions.get(projectId)?.emit({ type: "context", context });
}

function contextLine(c) {
  if (!c) return "";
  if (c.from && c.to) {
    const lines = c.from === c.to ? `line ${c.from} is selected` : `lines ${c.from} to ${c.to} are selected`;
    return `[In the editor ${c.path} is open, and ${lines}.]`;
  }
  return `[In the editor ${c.path} is open, with the cursor in line ${c.line || 1}.]`;
}

function instructions(name) {
  return [
    `You are the writing assistant of meertex, a LaTeX editor for research papers. You run on the computer of the person you talk to, in the folder of their checkout of the paper "${name}".`,
    "The meertex sync service sends every change to a file in this folder to the paper's live document within a second, and coauthors see it in their editors right away. So change only what the person asks for, keep the rest of the text as it is, and do not reformat whole files.",
    "Never touch the .meertex folder. It belongs to the sync client.",
    "A message can start with a line in brackets that says which file the person has open in the editor and which lines are selected. Line numbers start at 1.",
    "Build output (.aux, .log and the main PDF) is not synced, so you may compile with latexmk to check your changes.",
  ].join("\n");
}

// --- One chat per paper ---------------------------------------------------------------

class Session {
  constructor(projectId) {
    this.projectId = projectId;
    this.frames = new Set();
    this.window = null;
    this.clear();
  }

  clear() {
    this.items = [];
    this.byId = new Map();
    this.tools = new Map();
    this.declined = new Set();
    this.streamed = new Map();
    this.blocks = new Map();
    this.messageId = null;
    this.sessionId = null;
    this.busy = false;
    this.stopping = false;
    this.pending = new Map();
    this.provider = null;
    this.model = null;
  }

  async attach(e) {
    this.frames.add(e.senderFrame);
    this.window = BrowserWindow.fromWebContents(e.sender) || this.window;
    return this.state();
  }

  async state() {
    const st = await status(this.projectId);
    const checkout = findCheckout(this.projectId);
    return {
      ...st,
      checkout: checkout ? { ...st.checkout, name: checkout.name } : st.checkout,
      items: this.items,
      busy: this.busy,
      context: contexts.get(this.projectId) || null,
    };
  }

  emit(event) {
    for (const frame of this.frames) {
      if (frame.isDestroyed() || frame.detached || frame.origin !== PANEL_ORIGIN) {
        this.frames.delete(frame);
        continue;
      }
      try {
        frame.send("agent:event", event);
      } catch {
        this.frames.delete(frame);
      }
    }
  }

  add(item) {
    item.id = crypto.randomUUID();
    this.items.push(item);
    this.byId.set(item.id, item);
    while (this.items.length > MAX_ITEMS) this.byId.delete(this.items.shift().id);
    this.emit({ type: "item", item });
    return item;
  }

  update(item) {
    if (this.byId.has(item.id)) this.emit({ type: "item", item });
  }

  setBusy(busy) {
    this.busy = busy;
    this.emit({ type: "busy", busy });
  }

  // A path the agent used, relative to the checkout, or null outside it.
  relative(file) {
    const checkout = findCheckout(this.projectId);
    if (!checkout || !file) return null;
    const abs = path.resolve(checkout.dir, file);
    const rel = path.relative(checkout.dir, abs);
    if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) return null;
    const posix = rel.split(path.sep).join("/");
    return posix.startsWith(".meertex/") ? null : posix;
  }

  // The page's context goes into the prompt, so its path has to name a file
  // of the checkout. Otherwise a page could write instructions into it.
  fileContext(checkout) {
    const c = contexts.get(this.projectId);
    if (!c || /[\x00-\x1f[\]]/.test(c.path) || this.relative(c.path) !== c.path) return null;
    try {
      return fs.statSync(path.join(checkout.dir, c.path)).isFile() ? c : null;
    } catch {
      return null;
    }
  }

  async choose(e, choice) {
    const s = readSettings();
    const provider = PROVIDERS.includes(choice?.provider) ? choice.provider : s.provider;
    const model = isModelName(choice?.model) ? choice.model : null;
    if (provider === "claude" && model !== null && CLAUDE_MODELS.includes(model)) s.claudeModel = model;
    if (provider === "ollama" && model !== null) s.ollamaModel = model;
    s.provider = provider;
    saveSettings(s);
    // Another provider is another conversation. Another model of the same
    // one carries on from where the chat is, through --resume.
    if (this.provider && this.provider !== provider) this.reset();
    else if (this.proc && !this.busy) this.kill();
    return this.attach(e);
  }

  async send(e, text, withContext) {
    this.window = BrowserWindow.fromWebContents(e.sender) || this.window;
    if (this.busy || !text.trim()) return false;
    const checkout = findCheckout(this.projectId);
    if (!checkout) {
      this.add({ kind: "error", text: "This paper has no checkout on this computer. The Agent tab says how to make one." });
      return false;
    }
    const context = withContext ? this.fileContext(checkout) : null;
    this.add({ kind: "user", text, context: context ? context.path : null });
    this.setBusy(true);
    try {
      await this.start(checkout);
    } catch (err) {
      this.add({ kind: "error", text: err.message });
      this.setBusy(false);
      return false;
    }
    const line = contextLine(context);
    this.write({ type: "user", message: { role: "user", content: line ? `${line}\n\n${text}` : text }, parent_tool_use_id: null });
    return true;
  }

  async start(checkout) {
    if (this.proc) return;
    const s = readSettings();
    const provider = s.provider;
    const model = provider === "ollama" ? s.ollamaModel : s.claudeModel;
    const env = await shellEnv();
    const bin = findClaude(env);
    if (!bin) throw new Error("Claude Code is not installed, or meertex cannot find the claude command. The Agent tab says how to set it up.");
    if (provider === "ollama" && !model) throw new Error("Pick an Ollama model at the top of the panel first.");

    const args = ["--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
      "--include-partial-messages", "--permission-prompt-tool=stdio", "--permission-mode", "acceptEdits"];
    if (model) args.push("--model", model);
    if (this.sessionId) args.push("--resume", this.sessionId);
    const childEnv = { ...env };
    if (provider === "ollama") {
      // A local model gets short instructions. --bare leaves out hooks,
      // CLAUDE.md files and the long system prompt, and the model sees only
      // the tools for files and commands, without MCP servers and skills.
      // That takes the first request from tens of thousands of tokens to
      // about a thousand.
      args.push("--bare", "--tools", "Read,Edit,Write,Glob,Grep,Bash", "--strict-mcp-config", "--disable-slash-commands");
      // Ollama speaks Anthropic's Messages API (docs.ollama.com/api/anthropic-compatibility).
      // --bare takes only an API key, which Ollama does not check. Every
      // model Claude Code would pick for a side job is the chosen one, since
      // Ollama has no claude-haiku.
      delete childEnv.ANTHROPIC_AUTH_TOKEN;
      Object.assign(childEnv, {
        ANTHROPIC_BASE_URL: ollamaBase,
        ANTHROPIC_API_KEY: "ollama",
        ANTHROPIC_DEFAULT_OPUS_MODEL: model,
        ANTHROPIC_DEFAULT_SONNET_MODEL: model,
        ANTHROPIC_DEFAULT_HAIKU_MODEL: model,
        CLAUDE_CODE_SUBAGENT_MODEL: model,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
      });
    }

    const child = spawn(bin, args, { cwd: checkout.dir, env: childEnv, stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    this.proc = child;
    this.provider = provider;
    this.model = model;
    this.dir = checkout.dir;
    const stderr = [];
    child.stdin.on("error", () => {});
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      if (this.proc !== child) return;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        return;
      }
      try {
        this.handle(msg);
      } catch (err) {
        console.error("agent:", err);
      }
    });
    readline.createInterface({ input: child.stderr }).on("line", (line) => {
      stderr.push(line);
      if (stderr.length > STDERR_LINES) stderr.shift();
    });
    const ended = (text) => {
      if (this.proc !== child) return;
      this.proc = null;
      this.abortPending("stop");
      if (this.busy) {
        this.add({ kind: "error", text: [text, ...stderr.slice(-5)].filter(Boolean).join("\n") });
        this.finishTools("error");
        this.setBusy(false);
      }
    };
    child.on("error", (err) => ended(`Claude Code could not start. ${err.message}`));
    child.on("exit", (code, signal) => ended(`Claude Code stopped${signal ? ` (${signal})` : ` with exit code ${code}`}.`));
    this.write({ type: "control_request", request_id: crypto.randomUUID(), request: { subtype: "initialize", appendSystemPrompt: instructions(checkout.name) } });
  }

  write(msg) {
    try {
      this.proc?.stdin.write(`${JSON.stringify(msg)}\n`);
    } catch (err) {
      console.warn(`agent: could not write to Claude Code. ${err.message}`);
    }
  }

  handle(msg) {
    // A subagent's own messages carry the Task call they belong to. The chat
    // shows that call only.
    const sub = Boolean(msg.parent_tool_use_id);
    switch (msg.type) {
      case "system":
        if (msg.subtype === "init" && typeof msg.session_id === "string") this.sessionId = msg.session_id;
        break;
      case "stream_event":
        if (!sub) this.streamEvent(msg.event || {});
        break;
      case "assistant":
        if (!sub) this.assistant(msg.message || {});
        break;
      case "user":
        this.toolResults(msg.message || {});
        break;
      case "result":
        this.result(msg);
        break;
      case "control_request":
        this.control(msg);
        break;
      case "control_cancel_request":
        this.pending.get(msg.request_id)?.abort("cancel");
        break;
      default:
        break;
    }
  }

  // Text as it streams. The finished message comes again in full afterwards
  // (assistant), and replaces what streamed.
  streamEvent(ev) {
    if (ev.type === "message_start") {
      this.messageId = ev.message?.id || null;
      this.blocks = new Map();
    } else if (ev.type === "content_block_start" && ev.content_block?.type === "text") {
      const item = this.add({ kind: "assistant", text: ev.content_block.text || "" });
      this.blocks.set(ev.index, item);
      if (this.messageId) {
        if (!this.streamed.has(this.messageId)) this.streamed.set(this.messageId, []);
        this.streamed.get(this.messageId).push(item);
      }
    } else if (ev.type === "content_block_delta" && ev.delta?.type === "text_delta") {
      const item = this.blocks.get(ev.index);
      if (item) {
        item.text += ev.delta.text || "";
        this.update(item);
      }
    }
  }

  assistant(message) {
    const waiting = this.streamed.get(message.id) || [];
    for (const block of Array.isArray(message.content) ? message.content : []) {
      if (block.type === "text" && block.text) {
        const item = waiting.shift();
        if (item) {
          item.text = block.text;
          this.update(item);
        } else {
          this.add({ kind: "assistant", text: block.text });
        }
      } else if (block.type === "tool_use" && block.id && !this.tools.has(block.id)) {
        this.tools.set(block.id, this.add({ kind: "tool", status: "running", ...this.describeTool(block.name, block.input || {}) }));
      }
    }
  }

  toolResults(message) {
    for (const block of Array.isArray(message.content) ? message.content : []) {
      if (block?.type !== "tool_result") continue;
      const item = this.tools.get(block.tool_use_id);
      if (!item) continue;
      // A call the person declined gets Claude Code's long note for the
      // model as its result. The chat says "Declined" instead.
      if (this.declined.has(block.tool_use_id)) item.status = "declined";
      else {
        item.status = block.is_error ? "error" : "done";
        if (block.is_error) item.error = resultText(block.content).slice(0, 400);
      }
      this.update(item);
    }
  }

  result(msg) {
    if (this.stopping) this.add({ kind: "note", text: "Stopped." });
    else if (msg.is_error || msg.subtype !== "success") {
      const why = typeof msg.result === "string" && msg.result ? msg.result
        : Array.isArray(msg.errors) && msg.errors.length ? msg.errors.join(" ") : `The turn ended with ${msg.subtype}.`;
      this.add({ kind: "error", text: why });
    }
    this.stopping = false;
    this.streamed.clear();
    this.finishTools("stopped");
    this.setBusy(false);
  }

  finishTools(statusText) {
    for (const item of this.tools.values()) {
      if (item.status !== "running") continue;
      item.status = statusText;
      this.update(item);
    }
  }

  // One line per tool call: what it did, and the file when it is one of the
  // paper's (the panel opens it in the editor).
  describeTool(name, input) {
    const file = typeof input.file_path === "string" ? input.file_path : typeof input.notebook_path === "string" ? input.notebook_path : null;
    const rel = file ? this.relative(file) : null;
    const shown = rel || (file ? tilde(file) : null);
    const line = Number.isInteger(input.offset) && input.offset > 0 ? input.offset : null;
    switch (name) {
      case "Read": return { verb: "Read", target: shown, path: rel, line };
      case "Edit": case "MultiEdit": return { verb: "Edited", target: shown, path: rel };
      case "Write": return { verb: "Wrote", target: shown, path: rel };
      case "NotebookEdit": return { verb: "Edited", target: shown, path: rel };
      case "Bash": return { verb: "Ran", target: oneLine(input.command), code: true };
      case "Glob": return { verb: "Looked for", target: oneLine(input.pattern), code: true };
      case "Grep": return { verb: "Searched for", target: oneLine(input.pattern), code: true };
      case "WebFetch": return { verb: "Opened", target: oneLine(input.url) };
      case "WebSearch": return { verb: "Searched the web for", target: oneLine(input.query) };
      case "Task": case "Agent": return { verb: "Started a helper", target: oneLine(input.description) };
      case "TodoWrite": return { verb: "Updated its plan", target: null };
      default: return { verb: `Used ${name}`, target: null };
    }
  }

  control(msg) {
    const req = msg.request || {};
    if (req.subtype === "can_use_tool") {
      this.ask(msg.request_id, req);
      return;
    }
    this.write({ type: "control_response", response: { subtype: "error", request_id: msg.request_id, error: `meertex does not answer ${req.subtype} requests.` } });
  }

  // Claude Code asks before anything acceptEdits does not cover: commands,
  // web access, files outside the checkout. The answer is a native dialog of
  // this app, which no web page can draw or click.
  async ask(requestId, req) {
    const controller = new AbortController();
    this.pending.set(requestId, controller);
    const { message, detail } = askText(req, this.dir);
    const rules = req.suppress_always_allow_rule ? [] : (Array.isArray(req.permission_suggestions) ? req.permission_suggestions : [])
      .filter((s) => s && s.type === "addRules" && s.behavior === "allow")
      .map((s) => ({ ...s, destination: "session" }));
    const buttons = rules.length ? ["Allow", "Allow and don't ask again in this chat", "Deny"] : ["Allow", "Deny"];
    const deny = buttons.length - 1;
    let response = deny;
    try {
      const opts = {
        type: "question",
        buttons,
        defaultId: req.default_to_no ? deny : 0,
        cancelId: deny,
        noLink: true,
        title: this.provider === "ollama" ? "Agent (Ollama)" : "Agent (Claude Code)",
        message,
        detail,
        signal: controller.signal,
      };
      const win = this.window && !this.window.isDestroyed() ? this.window : null;
      ({ response } = win ? await dialog.showMessageBox(win, opts) : await dialog.showMessageBox(opts));
    } catch {
      response = deny;
    }
    this.pending.delete(requestId);
    // Claude Code withdrew the question. It wants no answer any more.
    if (controller.signal.aborted && controller.signal.reason === "cancel") return;
    const allowed = !controller.signal.aborted && response !== deny;
    if (!allowed && typeof req.tool_use_id === "string") this.declined.add(req.tool_use_id);
    this.write({
      type: "control_response",
      response: {
        subtype: "success",
        request_id: requestId,
        response: allowed
          ? { behavior: "allow", updatedInput: req.input || {}, ...(response === 1 && rules.length ? { updatedPermissions: rules } : {}) }
          : { behavior: "deny", message: "The person declined this in meertex.", ...(controller.signal.aborted ? { interrupt: true } : {}) },
      },
    });
  }

  abortPending(reason) {
    for (const controller of this.pending.values()) controller.abort(reason);
    this.pending.clear();
  }

  stop() {
    if (!this.busy || !this.proc) return false;
    this.stopping = true;
    this.abortPending("stop");
    this.write({ type: "control_request", request_id: crypto.randomUUID(), request: { subtype: "interrupt" } });
    return true;
  }

  async reset() {
    this.kill();
    this.clear();
    this.emit({ type: "reset" });
    return true;
  }

  kill() {
    const child = this.proc;
    this.proc = null;
    this.abortPending("stop");
    if (this.busy) this.setBusy(false);
    if (!child) return;
    try {
      child.stdin.end();
    } catch {
      // Gone already.
    }
    const timer = setTimeout(() => child.kill(), 3000);
    child.once("exit", () => clearTimeout(timer));
  }
}

function askText(req, dir) {
  const input = req.input || {};
  const where = (p) => (typeof p === "string" ? tilde(path.resolve(dir || os.homedir(), p)) : "");
  const reason = typeof req.decision_reason === "string" ? `\n\n${stripAnsi(req.decision_reason)}` : "";
  let message;
  let detail;
  switch (req.tool_name) {
    case "Bash":
      message = "Run this command?";
      detail = `${String(input.command || "")}${input.description ? `\n\n${input.description}` : ""}`;
      break;
    case "Edit": case "MultiEdit": case "Write": case "NotebookEdit":
      message = "Change this file?";
      detail = where(input.file_path || input.notebook_path);
      break;
    case "Read": case "Glob": case "Grep":
      message = "Look outside the paper's folder?";
      detail = where(input.file_path || input.path || "") + (input.pattern ? `\n${input.pattern}` : "");
      break;
    case "WebFetch":
      message = "Open this web page?";
      detail = String(input.url || "");
      break;
    case "WebSearch":
      message = "Search the web?";
      detail = String(input.query || "");
      break;
    default:
      message = `Use ${req.display_name || req.tool_name}?`;
      detail = JSON.stringify(input, null, 2);
  }
  return { message, detail: `${detail.slice(0, 2000)}${reason}`.trim() };
}

function resultText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((c) => (typeof c?.text === "string" ? c.text : "")).join("\n");
  return "";
}

function oneLine(value) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > 160 ? `${text.slice(0, 157)}...` : text;
}

function stripAnsi(text) {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "");
}

module.exports = { registerScheme, setup, PANEL_ORIGIN };
