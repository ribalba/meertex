/* The find bar's bridge to find.js. It runs only in the find bar's own view,
 * never in a page of the server. */
const { contextBridge, ipcRenderer } = require("electron");

function listen(channel, fn) {
  ipcRenderer.on(channel, (_e, payload) => fn(payload));
}

contextBridge.exposeInMainWorld("findBar", {
  search: (text, forward, again) => ipcRenderer.send("find:search", String(text), Boolean(forward), Boolean(again)),
  close: () => ipcRenderer.send("find:close"),
  onShow: (fn) => listen("find:show", fn),
  onStep: (fn) => listen("find:step", fn),
  onResult: (fn) => listen("find:result", fn),
});
