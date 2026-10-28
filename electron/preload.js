/* The bridge between the page and the desktop shell. It is almost empty on
 * purpose. The page is a remote origin and gets no Node, no IPC and no file
 * access from here.
 *
 * The page detects the shell by the presence of `window.meertexDesktop`. When
 * it is there, the sign-in page asks for its email with `client: "desktop"`
 * (POST /api/auth/login, app/static/js/login.js). The server then mails a
 * `meertex://login?token=...` link, which main.js turns into a sign-in inside
 * this window instead of in whatever browser the mail client would open.
 */
const { contextBridge } = require("electron");

// main.js passes its own version as an argument. A sandboxed preload cannot
// read package.json, but it does see its process's argv.
const PREFIX = "--meertex-shell-version=";
const versionArg = process.argv.find((arg) => arg.startsWith(PREFIX));

contextBridge.exposeInMainWorld("meertexDesktop", {
  // The shape of this bridge, for the page to check against if it ever grows.
  version: 1,
  // The shell's release, from electron/package.json.
  shellVersion: versionArg ? versionArg.slice(PREFIX.length) : null,
  platform: process.platform,
});
