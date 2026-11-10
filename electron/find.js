/* Find in page, which every browser has and Electron does not.
 *
 * Ctrl+F (Cmd+F on macOS) opens a find bar over the top right corner of the
 * window, as in a browser. It searches the whole page with Chromium's own
 * find, so it finds the text of the review threads, the source in a review's
 * Files view, the text layer of the PDF pages that are drawn, and so on.
 *
 * The bar is a page of this app (findbar/) in a view of its own over the
 * window, not an element of the server's page. So the page cannot script it,
 * Chromium's search does not find the text typed into it, and searching does
 * not take the focus away from it.
 *
 * The keys are a browser's. Enter and F3 (Cmd+G) go to the next match,
 * Shift+Enter and Shift+F3 (Cmd+Shift+G) to the one before, and Escape closes
 * the bar and leaves the match selected. The page sees a key before the menu
 * does (main.js, buildMenu), so in the editor's text Ctrl+F stays the editor's
 * own search and replace.
 */
const { BrowserWindow, WebContentsView, ipcMain } = require("electron");
const path = require("path");

const WIDTH = 380;
const HEIGHT = 44;
// Below the window's top edge, and far enough from the right one to leave
// the page's scroll bar free.
const TOP = 8;
const RIGHT = 24;
// Requests below this id belong to a search that is over.
const NO_SESSION = Number.MAX_SAFE_INTEGER;

// One bar per window, made on the first Ctrl+F in it.
const bars = new WeakMap();
// The bars by the id of their own web contents, to know who sends a message.
const bySender = new Map();

// A search belongs to one page. The app moves between pages with pushState,
// so a different path or query means different text, while a new hash or the
// router saving the scroll position does not.
function pageKey(url) {
  try {
    const u = new URL(url);
    return u.origin + u.pathname + u.search;
  } catch {
    return url;
  }
}

class FindBar {
  constructor(win) {
    this.win = win;
    this.page = win.webContents;
    this.view = null;
    this.ready = null;
    this.visible = false;
    // The text, page and first request of the search that is running.
    this.text = "";
    this.key = null;
    this.session = NO_SESSION;
    this.active = 0;
    this.matches = 0;

    this.page.on("found-in-page", (_e, result) => this.found(result));
    // A new page drops the old count. The next Enter searches the new one.
    this.page.on("did-navigate", () => this.forget());
    this.page.on("did-navigate-in-page", (_e, url, isMainFrame) => {
      if (isMainFrame && this.key && pageKey(url) !== this.key) this.forget();
    });
    const layout = () => this.layout();
    for (const event of ["resize", "maximize", "unmaximize", "enter-full-screen", "leave-full-screen"]) {
      win.on(event, layout);
    }
    win.once("closed", () => this.destroy());
  }

  // The view and its page are made on the first open, not with the window.
  create() {
    this.view = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        preload: path.join(__dirname, "findbar", "preload.js"),
        spellcheck: false,
      },
    });
    this.view.setBackgroundColor("#ffffff");
    this.view.setBorderRadius(6);
    this.view.setVisible(false);
    this.win.contentView.addChildView(this.view);
    const contents = this.view.webContents;
    bySender.set(contents.id, this);
    contents.once("destroyed", () => bySender.delete(contents.id));
    this.ready = contents.loadFile(path.join(__dirname, "findbar", "index.html")).catch((err) => {
      console.warn("find: the find bar did not load", err);
    });
  }

  layout() {
    if (!this.view) return;
    const [width] = this.win.getContentSize();
    const w = Math.max(0, Math.min(WIDTH, width - 2 * RIGHT));
    this.view.setBounds({ x: Math.max(0, width - w - RIGHT), y: TOP, width: w, height: HEIGHT });
  }

  // Ctrl+F. Opens the bar, or selects its text when it is open already. On
  // opening, a text from before is searched again, as in a browser.
  open() {
    if (!this.view) this.create();
    const reopened = !this.visible;
    this.visible = true;
    this.layout();
    this.view.setVisible(true);
    this.ready.then(() => {
      if (!this.visible || this.view.webContents.isDestroyed()) return;
      this.view.webContents.focus();
      this.send("find:show", { search: reopened });
    });
  }

  // F3 and Shift+F3. The bar holds the text, so it runs the step itself.
  step(forward) {
    if (!this.visible) {
      this.open();
      return;
    }
    this.send("find:step", { forward });
  }

  // From the bar. `again` is Enter or an arrow, which moves to the next match
  // of the same search. Typing starts a new search.
  search(text, forward, again) {
    if (!this.visible) return;
    if (!text) {
      this.page.stopFindInPage("clearSelection");
      this.forget();
      return;
    }
    const key = pageKey(this.page.getURL());
    const fresh = !again || text !== this.text || key !== this.key;
    if (fresh) {
      this.text = text;
      this.key = key;
      this.active = 0;
      this.matches = 0;
    }
    // findNext starts a new search session when true (Electron's docs).
    const id = this.page.findInPage(text, { forward, findNext: fresh });
    if (fresh) this.session = id;
  }

  found(result) {
    if (!this.visible || result.requestId < this.session) return;
    if (result.matches >= 0) this.matches = result.matches;
    if (result.activeMatchOrdinal >= 0) this.active = result.activeMatchOrdinal;
    this.active = Math.min(this.active, this.matches);
    this.send("find:result", { active: this.active, matches: this.matches });
  }

  // Ends the running search. The bar keeps its text and shows no count.
  forget() {
    this.text = "";
    this.key = null;
    this.session = NO_SESSION;
    this.active = 0;
    this.matches = 0;
    this.send("find:result", null);
  }

  // Escape or the close button. The current match stays selected, so Ctrl+C
  // copies it, and the focus goes back to the page.
  close() {
    if (!this.visible) return;
    this.page.stopFindInPage("keepSelection");
    this.forget();
    this.visible = false;
    this.view.setVisible(false);
    this.page.focus();
  }

  send(channel, payload) {
    const contents = this.view?.webContents;
    if (contents && !contents.isDestroyed()) contents.send(channel, payload);
  }

  // A view's page is not closed with its window.
  destroy() {
    const contents = this.view?.webContents;
    if (contents && !contents.isDestroyed()) contents.close();
    this.view = null;
    this.visible = false;
  }
}

// The window of a menu click, or nothing when it has no page to search.
function barOf(win) {
  if (!(win instanceof BrowserWindow) || win.isDestroyed()) return null;
  let bar = bars.get(win);
  if (!bar) {
    bar = new FindBar(win);
    bars.set(win, bar);
  }
  return bar;
}

function fromBar(e) {
  return bySender.get(e.sender.id) || null;
}

function setup() {
  ipcMain.on("find:search", (e, text, forward, again) => {
    fromBar(e)?.search(String(text || "").slice(0, 1000), forward !== false, Boolean(again));
  });
  ipcMain.on("find:close", (e) => fromBar(e)?.close());
}

// The Edit menu's part. The accelerators are the browsers' own.
function menuItems(isMac) {
  return [
    { label: "Find…", accelerator: "CmdOrCtrl+F", click: (_item, win) => barOf(win)?.open() },
    { label: "Find Next", accelerator: isMac ? "Cmd+G" : "F3", click: (_item, win) => barOf(win)?.step(true) },
    { label: "Find Previous", accelerator: isMac ? "Cmd+Shift+G" : "Shift+F3", click: (_item, win) => barOf(win)?.step(false) },
  ];
}

module.exports = { setup, menuItems };
