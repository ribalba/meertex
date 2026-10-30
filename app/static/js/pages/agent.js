// The Agent tab. It says how to get the desktop app's agent panel for this
// paper (DESIGN §20): the checkout it works in, the sync that brings its edits
// back, and Claude Code or Ollama. Inside the desktop app it also checks each
// of these on this computer.

import { h, clear } from "../dom.js";
import { icon } from "../icons.js";
import { navigate } from "../router.js";
import { commandLine, flashBox, setTitle, spinner } from "../ui.js";
import { desktopAgent } from "../editor-agent.js";
import { editorHref } from "./editor.js";

const RELEASES = "https://github.com/ribalba/meertex/releases/latest";
const CLAUDE_DOCS = "https://code.claude.com/docs/en/setup";
const OLLAMA_DOCS = "https://docs.ollama.com/integrations/claude-code";

export async function render(ctx) {
  const p = ctx.project;
  setTitle("Agent", p.name);
  const bridge = desktopAgent();

  const head = h("div", { class: "subhead" },
    h("h2", { text: "Agent" }),
    h("span", { class: "text-small color-muted", text: "Write with Claude or a local model next to the text, in the desktop app." }));

  const side = bridge ? h("div", { class: "box agent-checks" }, h("div", { class: "box-body color-muted" }, spinner(), " Checking this computer…")) : null;

  clear(ctx.main).append(h("div", { class: "agent-page" },
    head,
    ctx.canEdit ? null : flashBox("The agent edits the paper, so it is there for the owner and the editors. The owner can make you an editor.", "warn"),
    bridge ? null : window.meertexDesktop
      ? flashBox(h("span", {},
        "This desktop app is older than the agent. ",
        h("a", { href: RELEASES, target: "_blank", rel: "noopener", text: "Install the latest release" }),
        " and open this tab again."), "warn")
      : flashBox(h("span", {},
        "The agent runs in the desktop app, because it uses programs on your computer. ",
        h("a", { href: RELEASES, target: "_blank", rel: "noopener", text: "Download the desktop app" }),
        " for Linux, macOS or Windows."), "info"),
    side,
    howItWorks(),
    setup(p),
    withOllama(),
    placing(),
    goodToKnow(),
  ));

  if (bridge) await paintChecks(ctx, bridge, side);
}

// --- On this computer ----------------------------------------------------------------

async function paintChecks(ctx, bridge, box) {
  const p = ctx.project;
  let st;
  try {
    st = await bridge.status(p.id);
  } catch {
    st = null;
  }
  if (!ctx.alive()) return;
  if (!st) {
    box.replaceChildren(h("div", { class: "box-body" }, "The desktop app did not answer. Restart it and open this tab again."));
    return;
  }

  const checkout = st.checkout || {};
  const ollama = st.ollama || {};
  const tools = (ollama.models || []).filter((m) => m.tools);
  const rows = [
    check(true, "Desktop app", `meertex ${window.meertexDesktop.shellVersion || ""}`.trim()),
    check(checkout.found, "Checkout of this paper",
      checkout.found ? checkout.folder : h("span", {}, "None on this computer yet. Make one with ", h("code", { text: `meertex checkout ${p.name}` }), ".")),
    check(checkout.found && checkout.syncing, "Sync",
      !checkout.found ? "Needs the checkout first."
        : checkout.syncing ? (checkout.held ? "Running, and the checkout is held (meertex release sends the held edits)." : checkout.connected ? "Running and connected." : "Running, not connected right now.")
          : h("span", {}, "Not running, so the agent's edits stay in the folder. Start it with ", h("code", { text: "meertex sync" }), ".")),
    check(st.claude?.found, "Claude Code",
      st.claude?.found ? `Version ${st.claude.version || "unknown"}.` : "Not found. Install it as below, then open this tab again."),
    check(ollama.running && ollama.recent && tools.length ? true : ollama.running ? false : null, "Ollama (optional)",
      !ollama.running ? "Not running. You need it only for a local model."
        : !ollama.recent ? `Version ${ollama.version} is too old. Claude Code needs 0.14 or newer.`
          : tools.length ? `Version ${ollama.version || "unknown"}, with ${tools.map((m) => m.name).join(", ")}.`
            : "Running, but no model that can call tools is pulled yet."),
  ];
  const ready = checkout.found && st.claude?.found;
  box.replaceChildren(
    h("div", { class: "box-header box-header-sm" }, h("h3", { class: "box-title", text: "On this computer" }),
      h("button", { type: "button", class: "btn btn-sm", text: "Check again", onclick: () => paintChecks(ctx, bridge, box) })),
    h("ul", { class: "box-list" }, rows),
    h("div", { class: "box-footer agent-open" },
      h("button", {
        type: "button", class: `btn${ready && ctx.canEdit ? " btn-primary" : ""}`, disabled: !ctx.canEdit,
        onclick: () => openEditor(ctx),
      }, icon("sparkle"), "Open the agent in the editor"),
      h("span", { class: "text-small color-muted", text: ready ? "Everything it needs is there." : "The panel says what is still missing." })),
  );
}

// `ok` is true, false, or null for something optional that is not there.
function check(ok, title, text) {
  const name = ok ? "check-circle" : ok === null ? "dot" : "x-circle";
  return h("li", { class: `box-row agent-check ${ok ? "is-ok" : ok === null ? "is-off" : "is-missing"}` },
    icon(name),
    h("div", {}, h("div", { class: "text-bold", text: title }), h("div", { class: "text-small color-muted" }, text)));
}

// The editor opens the panel where it was last, as a column the first time.
function openEditor(ctx) {
  try {
    if (!["column", "below"].includes(localStorage.getItem("meertex.editor.agent"))) {
      localStorage.setItem("meertex.editor.agent", localStorage.getItem("meertex.editor.agentlast") === "below" ? "below" : "column");
    }
  } catch {
    // Without storage the editor opens without the panel. Its Agent button opens it.
  }
  navigate(editorHref(ctx.base));
}

// --- The text ------------------------------------------------------------------------

function section(title, ...body) {
  return h("section", { class: "agent-section" }, h("h3", { text: title }), ...body);
}

function howItWorks() {
  return section("How it works",
    h("p", { text: "The agent is the Claude Code of your own computer, signed in with your own account. With Ollama, Claude Code talks to a model on your computer instead, and the paper does not leave it." }),
    h("p", { text: "It works in your checkout of the paper. meertex sync carries its edits into the live document within a second, so they show up in the editor like a coauthor's typing, for you and for everyone else. Every change ends up in the History." }),
    h("p", { text: "It knows which file you have open and which lines you selected. A box above the message field turns that off for a message." }),
    h("p", { text: "It changes the paper's files without asking. Before it runs a command, opens a web page or looks outside the paper's folder, the desktop app asks you in a dialog of its own." }));
}

function setup(p) {
  const step = (title, ...body) => h("li", {}, h("div", { class: "text-bold", text: title }), ...body);
  return section("Set it up",
    h("ol", { class: "agent-steps" },
      step("Install the desktop app",
        h("p", {}, "From the ", h("a", { href: RELEASES, target: "_blank", rel: "noopener", text: "latest release" }), ", and sign in there.")),
      step("Check the paper out",
        h("p", { text: "The agent works in a folder on your computer. The meertex command line tool makes it." }),
        h("div", { class: "cmd-list" },
          commandLine("pipx install meertex", "Install the command line tool"),
          commandLine("meertex login", "Sign in with your email"),
          commandLine(`meertex checkout ${p.name}`, "Copy the paper to ~/papers"))),
      step("Keep it in sync",
        h("p", { text: "The sync sends the agent's edits to the server, and everybody else's edits to the folder." }),
        h("div", { class: "cmd-list" },
          commandLine("meertex service install", "Run the sync in the background, also after a restart"),
          commandLine("meertex sync", "Or run it in a terminal while you work"))),
      step("Install Claude Code and sign in once",
        h("p", {}, "The ", h("a", { href: CLAUDE_DOCS, target: "_blank", rel: "noopener", text: "Claude Code setup guide" }), " has every way to install it. Signing in needs a Claude plan or an Anthropic API key."),
        h("div", { class: "cmd-list" },
          commandLine("curl -fsSL https://claude.ai/install.sh | bash", "Linux and macOS"),
          commandLine("irm https://claude.ai/install.ps1 | iex", "Windows, in PowerShell"),
          commandLine("claude", "Start it once in a terminal and sign in"))),
      step("Open the agent",
        h("p", {}, "Open the editor and press ", h("strong", { text: "Agent" }), " in its toolbar."))));
}

function withOllama() {
  return section("With a local model in Ollama",
    h("p", {}, "The panel can run Claude Code against ", h("a", { href: "https://ollama.com", target: "_blank", rel: "noopener", text: "Ollama" }),
      " instead of Anthropic's servers. Claude Code still has to be installed, but it needs no sign-in for this. Ollama's ",
      h("a", { href: OLLAMA_DOCS, target: "_blank", rel: "noopener", text: "guide for Claude Code" }), " has more."),
    h("ol", { class: "agent-steps" },
      h("li", {}, h("p", { text: "Install Ollama 0.14 or newer. Older versions do not speak Anthropic's API." })),
      h("li", {}, h("p", { text: "Pull a model that can call tools, for example qwen3-coder or gpt-oss." }),
        h("div", { class: "cmd-list" }, commandLine("ollama pull qwen3-coder"))),
      h("li", {}, h("p", { text: "Give it a context of at least 32k tokens. Ollama often starts a model with 4k, and then a single .tex file pushes out what the model was asked." }),
        h("div", { class: "cmd-list" }, commandLine("OLLAMA_CONTEXT_LENGTH=65536 ollama serve", "Or set the context length in Ollama's settings"))),
      h("li", {}, h("p", { text: "Pick Ollama and the model at the top of the panel." }))),
    h("p", { class: "color-muted", text: "For Ollama the panel starts Claude Code in its minimal mode, with only the tools for files and commands and without your CLAUDE.md, MCP servers and skills, so the instructions stay at about a thousand tokens. A small model on a computer without a graphics card still takes minutes for an answer and makes more mistakes than Claude." }));
}

function placing() {
  return section("Where the panel goes",
    h("p", { text: "It opens as a fourth column next to the PDF. The two buttons at the top of the panel move it under the text and back, and the cross closes it. Drag its edge to make it wider or taller. The editor remembers the place and the size." }));
}

function goodToKnow() {
  return section("Good to know",
    h("ul", {},
      h("li", { text: "The chat lives in the desktop app. It stays when you move the panel or reload the page, and it ends when you close the app. New chat starts over." }),
      h("li", { text: "With Claude, your own Claude Code settings apply, like your CLAUDE.md, your permission rules and your MCP servers. What your CLAUDE.md asks for, Claude does here too." }),
      h("li", {}, "For a bigger rewrite your coauthors should not see half done, hold the checkout first with ",
        h("code", { text: "meertex hold" }), " and send it all with ", h("code", { text: "meertex release" }), "."),
      h("li", { text: "Each paper has a chat of its own. The panel of another paper starts with an empty one." })));
}
