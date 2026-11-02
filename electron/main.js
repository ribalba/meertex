/* meertex desktop shell.
 *
 * A thin Electron wrapper around a meertex server, in meerpad's shape because
 * the two are hosted the same way. The server is https://meertex.com unless told
 * otherwise. The shell keeps its login in a persistent session of its own,
 * finishes the emailed sign-in inside the window through a `meertex://` deep
 * link (see handleDeepLink), and opens every other site in the system browser.
 *
 * Point it at another server with MEERTEX_URL:
 *   MEERTEX_URL=http://localhost:8060 npm start
 *
 * Override the spellchecker languages with MEERTEX_SPELLCHECK_LANGS:
 *   MEERTEX_SPELLCHECK_LANGS=en-GB,fr npm start
 *
 * The editor's agent panel, which runs the local `claude` command in the
 * paper's checkout, is in agent.js.
 */
const { app, BrowserWindow, Menu, dialog, session, shell } = require("electron");
const path = require("path");
const agent = require("./agent");

const APP_URL = (process.env.MEERTEX_URL || "https://meertex.com").replace(/\/+$/, "");
const APP_ORIGIN = new URL(APP_URL).origin;
// /login, not /. A signed-out visitor gets the landing page at "/", and /login
// sends a window that is already signed in on to the paper list (app/main.py).
const START_URL = `${APP_URL}/login`;
const PROTOCOL = "meertex";
// Named, so the session cookie survives a restart. Separate from your
// browser's. Signing in here does not sign the browser in, and the other way
// round.
const PARTITION = "persist:meertex";
const ICON = path.join(__dirname, "build", "icon.png");

// The editor underlines with the server's LanguageTool, and the browser's
// spellchecker only runs where the page turns it on (comments, or the editor's
// Spellcheck button on a server without LanguageTool). Chromium checks every
// language in this list at once, as in meerail and meercal.
const SPELLCHECK_LANGS = (process.env.MEERTEX_SPELLCHECK_LANGS || "en-US,de-DE")
  .split(",").map((l) => l.trim()).filter(Boolean);

// Linux shells match a window to its installed icon by name. X11 uses WM_CLASS
// (from the app name), Wayland uses app_id (from desktopName in package.json)
// against StartupWMClass in meertex.desktop. Same as meerpad. Both stay
// meertex, even though the command is meertex-gui.
app.setName("meertex");

let mainWindow = null;
// A sign-in link that arrived before there was a window to put it in. The
// first launch on Windows and Linux carries it in argv, and macOS can deliver
// open-url before the app is ready.
let pendingLink = findLink(process.argv);

function findLink(argv) {
  return (argv || []).find((a) => typeof a === "string" && a.startsWith(`${PROTOCOL}://`)) || null;
}

// Absolute URLs only, with no base to resolve against. With one, "" and the
// "null" origin of a sandboxed frame or a data: page would both resolve to
// this server and pass.
function isInternal(targetUrl) {
  try {
    return new URL(targetUrl).origin === APP_ORIGIN;
  } catch {
    return false;
  }
}

// Only schemes a link in a paper, a comment or a reference may carry.
// shell.openExternal hands the string to the OS, and the OS will happily run
// whatever a file:// or custom-scheme URL names.
function openExternal(targetUrl) {
  let parsed;
  try {
    parsed = new URL(targetUrl);
  } catch {
    return;
  }
  if (["http:", "https:", "mailto:"].includes(parsed.protocol)) shell.openExternal(parsed.toString());
}

function showWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    // Wide enough for the editor and the PDF side by side.
    width: 1440,
    height: 900,
    minWidth: 480,
    minHeight: 480,
    // The web app is white and has no dark theme, so the frame is white too.
    backgroundColor: "#ffffff",
    title: "meertex",
    icon: ICON,
    webPreferences: {
      // Loading a remote origin. Keep the page away from Node entirely.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
      partition: PARTITION,
      spellcheck: true,
      // preload.js also runs in the agent panel, an iframe of the editor. It
      // hands each frame its own bridge, by origin.
      nodeIntegrationInSubFrames: true,
      // Read back by preload.js, which in a sandbox cannot open package.json.
      additionalArguments: [`--meertex-shell-version=${app.getVersion()}`],
    },
  });

  if (pendingLink) {
    const link = pendingLink;
    pendingLink = null;
    handleDeepLink(link);
  } else {
    mainWindow.loadURL(START_URL);
  }

  // An unreachable server gets a retry screen instead of a blank window. -3 is
  // the aborted load every redirect produces. A failing iframe is the page's
  // problem and no reason to replace the whole app.
  mainWindow.webContents.on("did-fail-load", (_e, errorCode, _desc, validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3 || !isInternal(validatedURL || APP_URL)) return;
    mainWindow.loadURL(errorPage());
  });

  mainWindow.on("closed", () => { mainWindow = null; });
}

/* Navigation rules for every web contents, the main window's and any child
 * window's alike (Electron's security checklist). A rule set on one window
 * only is a rule a child window does not have. */
app.on("web-contents-created", (_e, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    // A compiled PDF, a version's PDF, a compile log or a raw file opens in a
    // new tab. They live on this server and need this session's cookie, so
    // they get a window of their own in here. Chromium's PDF viewer shows the
    // PDFs. Anything else, a DOI or a conference page, goes to the browser.
    if (isInternal(url)) {
      return { action: "allow", overrideBrowserWindowOptions: { icon: ICON, autoHideMenuBar: true } };
    }
    openExternal(url);
    return { action: "deny" };
  });

  contents.on("will-navigate", (e, url) => {
    if (url.startsWith(`${PROTOCOL}://`)) {
      e.preventDefault();
      handleDeepLink(url);
    } else if (!isInternal(url)) {
      e.preventDefault();
      openExternal(url);
    }
  });

  contents.on("will-prevent-unload", (e) => confirmUnload(contents, e));
  contents.on("context-menu", (_e, params) => showContextMenu(contents, params));
});

/* The editor holds the page open while it has edits the server has not seen
 * yet, which happens only offline (app/static/js/pages/editor.js). A browser
 * asks the user what to do. Electron silently keeps the page, so the close
 * button, Reload or Home would seem to do nothing. Ask the browser's question
 * instead. preventDefault on this event is what lets the page go. */
function confirmUnload(contents, event) {
  const choice = dialog.showMessageBoxSync(BrowserWindow.fromWebContents(contents), {
    type: "warning",
    buttons: ["Leave", "Stay"],
    defaultId: 1,
    cancelId: 1,
    title: "Unsent edits",
    message: "Some of your edits have not reached the server yet.",
    detail: "They are sent when the connection is back. If you leave now, they are lost.",
  });
  if (choice === 0) event.preventDefault();
}

/* Electron shows no context menu at all by default, which means no spelling
 * suggestions and no Paste on right-click. This is the browser's menu, reduced
 * to what meertex needs. A page that draws its own cancels the event, and then
 * this never runs. */
function showContextMenu(contents, params) {
  const groups = [];
  if (params.misspelledWord) {
    groups.push([
      ...params.dictionarySuggestions.slice(0, 5).map((word) => ({
        label: word,
        click: () => contents.replaceMisspelling(word),
      })),
      {
        label: "Add to dictionary",
        click: () => contents.session.addWordToSpellCheckerDictionary(params.misspelledWord),
      },
    ]);
  }
  if (params.linkURL && !isInternal(params.linkURL)) {
    groups.push([{ label: "Open link in browser", click: () => openExternal(params.linkURL) }]);
  }
  if (params.isEditable) {
    groups.push([{ role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" }]);
  } else if (params.selectionText) {
    groups.push([{ role: "copy" }]);
  }
  if (!groups.length) return;
  const template = groups.flatMap((group, i) => (i ? [{ type: "separator" }, ...group] : group));
  Menu.buildFromTemplate(template).popup();
}

/* Permissions a page asks for (the clipboard, for the copy buttons next to a
 * token or a share link) are granted to this server's pages and refused to
 * anything else that ends up in a frame here. Fullscreen is the exception,
 * because an embedded video player asks for it and nobody minds. */
function setPermissions() {
  const ses = session.fromPartition(PARTITION);
  ses.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(permission === "fullscreen" || isInternal(details.requestingUrl || contents.getURL()));
  });
  ses.setPermissionCheckHandler((_contents, permission, requestingOrigin) =>
    permission === "fullscreen" || isInternal(requestingOrigin));
}

/* macOS uses the OS spellchecker, which manages its own languages. Setting the
 * list there is a no-op, so it is skipped. Elsewhere Chromium fetches each
 * Hunspell dictionary on first use, so a language whose dictionary has not
 * arrived yet is simply not checked. */
function setSpellCheckLanguages() {
  if (process.platform === "darwin") return;
  const ses = session.fromPartition(PARTITION);
  const available = ses.availableSpellCheckerLanguages;
  const langs = SPELLCHECK_LANGS.filter((l) => available.includes(l));
  const unknown = SPELLCHECK_LANGS.filter((l) => !available.includes(l));
  if (unknown.length) {
    console.warn(`spellcheck: ignoring unsupported language(s) ${unknown.join(", ")}`);
  }
  if (langs.length) ses.setSpellCheckerLanguages(langs);
}

// --- Sign-in by deep link ------------------------------------------------------
// The login email, when requested from this shell (the page sends
// client: "desktop", see app/routers/auth.py), links to
//   meertex://login?token=<token>
// The OS routes that here, and sign-in finishes by loading the ordinary
// callback URL inside this window. The session cookie then lands in this app's
// partition rather than in the browser the mail client would have opened.
function handleDeepLink(link) {
  let parsed;
  try {
    parsed = new URL(link);
  } catch {
    return;
  }
  // Only ever the login action, and only to drive the callback. A link is
  // something anybody can put in a mail.
  if (parsed.protocol !== `${PROTOCOL}:` || parsed.hostname !== "login") return;
  const token = parsed.searchParams.get("token");
  if (!token) return;
  if (!mainWindow) {
    pendingLink = link;
    return;
  }
  mainWindow.loadURL(`${APP_URL}/api/auth/callback?token=${encodeURIComponent(token)}`);
  showWindow();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function errorPage() {
  const retry = JSON.stringify(START_URL);
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8" />
    <style>
      body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",sans-serif;background:#f6f8fa;color:#1f2328}
      .card{text-align:center;max-width:460px;padding:2rem}
      h1{font-size:1.3rem;margin:0 0 .5rem}p{color:#59636e;line-height:1.5}
      button{font:inherit;font-weight:600;cursor:pointer;border:1px solid rgba(31,35,40,.15);border-radius:6px;
        padding:.5rem 1rem;background:#1f883d;color:#fff;margin-top:1rem}
      code{background:#eff2f5;padding:.15rem .4rem;border-radius:6px}
    </style></head><body><div class="card">
      <h1>Can't reach meertex</h1>
      <p>Couldn't connect to <code>${escapeHtml(APP_ORIGIN)}</code>. Check your
      connection, or whether the server is up, and try again.</p>
      <button onclick="location.href=${escapeHtml(retry)}">Retry</button>
    </div>
    <script>addEventListener("online", () => { location.href = ${retry}; });</script>
    </body></html>`;
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

function goBack() {
  const history = mainWindow && mainWindow.webContents.navigationHistory;
  if (history && history.canGoBack()) history.goBack();
}

function goForward() {
  const history = mainWindow && mainWindow.webContents.navigationHistory;
  if (history && history.canGoForward()) history.goForward();
}

/* The page sees a key before the menu does, and a menu accelerator fires only
 * when the page did not handle the key (tested on Linux with Electron 44, not
 * on macOS). So Ctrl+Z stays the editor's own undo, and Alt+Left keeps its
 * editor meaning while the cursor is in the text, as in a browser. */
function buildMenu() {
  const isMac = process.platform === "darwin";
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(isMac ? [{ role: "appMenu" }] : []),
    { role: "fileMenu" },
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { label: "Home", accelerator: "CmdOrCtrl+Shift+H",
          click: () => mainWindow && mainWindow.loadURL(START_URL) },
        // There is no browser toolbar, and the app's pages make a history
        // worth walking back through. The keys are the browsers' own.
        { label: "Back", accelerator: isMac ? "Cmd+[" : "Alt+Left", click: goBack },
        { label: "Forward", accelerator: isMac ? "Cmd+]" : "Alt+Right", click: goForward },
        { type: "separator" },
        { role: "reload" }, { role: "forceReload" }, { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" },
        { role: "togglefullscreen" }, { role: "toggleDevTools" },
      ],
    },
    { role: "windowMenu" },
  ]));
}

function registerProtocol() {
  if (process.defaultApp && process.argv.length >= 2) {
    // `electron .` during development. Point the scheme at this checkout.
    app.setAsDefaultProtocolClient(PROTOCOL, process.execPath, [path.resolve(process.argv[1])]);
  } else {
    app.setAsDefaultProtocolClient(PROTOCOL);
  }
}

agent.registerScheme();

// Single instance. A second launch (a deep link opened on Windows or Linux)
// hands its argv to the running app instead of starting another.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => {
    const link = findLink(argv);
    if (link) handleDeepLink(link);
    showWindow();
  });

  // macOS delivers deep links here, possibly before the window exists.
  app.on("open-url", (e, url) => {
    e.preventDefault();
    if (mainWindow) handleDeepLink(url);
    else pendingLink = url;
  });

  app.whenReady().then(() => {
    registerProtocol();
    setPermissions();
    agent.setup(session.fromPartition(PARTITION), APP_ORIGIN);
    setSpellCheckLanguages();
    buildMenu();
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
