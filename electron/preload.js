/* The bridges between the pages and the desktop shell. They are small on
 * purpose. The server's pages are a remote origin and get no Node and no file
 * access from here.
 *
 * This script runs in every frame (nodeIntegrationInSubFrames, for the agent
 * panel), and each frame gets at most one bridge:
 *
 * - The top frame of a page of the server gets `window.meertexDesktop`. The
 *   page detects the shell by it. When it is there, the sign-in page asks for
 *   its email with `client: "desktop"` (POST /api/auth/login,
 *   app/static/js/login.js). The server then mails a `meertex://login?token=...`
 *   link, which main.js turns into a sign-in inside this window instead of in
 *   whatever browser the mail client would open. Its `agent` part feeds the
 *   agent panel the open file and selection, reports the status for the Agent
 *   tab, and passes on the panel's requests to open a file or to move or
 *   close the panel (agent.js). It cannot send a prompt.
 * - The agent panel (meertex-agent://panel, in an iframe of the editor) gets
 *   `window.meertexAgent`, which runs the chat.
 * - Every other frame gets nothing.
 *
 * agent.js checks the sending frame's origin of every message again.
 */
const { contextBridge, ipcRenderer } = require("electron");

const PANEL_ORIGIN = "meertex-agent://panel";

// main.js passes its own version as an argument. A sandboxed preload cannot
// read package.json, but it does see its process's argv.
const PREFIX = "--meertex-shell-version=";
const versionArg = process.argv.find((arg) => arg.startsWith(PREFIX));

function listen(channel, fn) {
  const listener = (_e, ...args) => fn(...args);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

if (location.origin === PANEL_ORIGIN) {
  contextBridge.exposeInMainWorld("meertexAgent", {
    attach: (projectId) => ipcRenderer.invoke("agent:attach", projectId),
    send: (projectId, text, withContext) => ipcRenderer.invoke("agent:send", projectId, text, withContext),
    stop: (projectId) => ipcRenderer.invoke("agent:stop", projectId),
    reset: (projectId) => ipcRenderer.invoke("agent:reset", projectId),
    choose: (projectId, choice) => ipcRenderer.invoke("agent:settings", projectId, choice),
    openFile: (projectId, path, line) => ipcRenderer.send("agent:open", projectId, path, line),
    layout: (projectId, action) => ipcRenderer.send("agent:layout", projectId, action),
    onEvent: (fn) => listen("agent:event", fn),
  });
} else if (window.top === window && /^https?:$/.test(location.protocol)) {
  contextBridge.exposeInMainWorld("meertexDesktop", {
    // The shape of this bridge, for the page to check against. 2 added `agent`.
    version: 2,
    // The shell's release, from electron/package.json.
    shellVersion: versionArg ? versionArg.slice(PREFIX.length) : null,
    platform: process.platform,
    agent: {
      panelOrigin: PANEL_ORIGIN,
      status: (projectId) => ipcRenderer.invoke("agent:status", projectId),
      setContext: (projectId, context) => ipcRenderer.send("agent:context", projectId, context),
      onOpenFile: (fn) => listen("agent:open-file", fn),
      onLayout: (fn) => listen("agent:layout", fn),
    },
  });
}
