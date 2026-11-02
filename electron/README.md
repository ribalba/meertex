# meertex desktop

A thin Electron wrapper that opens meertex in a native window. It is a shell
around a **running server**, the hosted [meertex.com](https://meertex.com) by
default or your own, and bundles no backend.

It has the same shape as meerpad's shell, because the two are hosted the same
way. It keeps a persistent login of its own (separate from your browser), opens
outbound links in the system browser and signs in by `meertex://` deep link.
The window chrome, menu and retry screen match the other meer* desktop apps.

## Run in development

```bash
cd electron
npm install
npm start                                     # loads https://meertex.com/login
MEERTEX_URL=http://localhost:8060 npm start   # a local server (`make up` in the repo)
```

`make desktop` in the repo root does the second line for you.

## Build and install

```bash
npm run dist        # -> dist/  (Linux .AppImage/.deb, macOS .dmg/.zip, Windows .exe)
make distinstall    # build, then register with the desktop (KDE / GNOME / macOS)
make distuninstall  # remove it again
```

The files have fixed names (`meertex-mac-arm64.dmg`, `meertex-windows-setup.exe`,
`meertex-linux-x86_64.AppImage`, ...), set by `artifactName` in `package.json`.

On Linux `make distinstall` copies the AppImage to `~/.local/share/meertex/`,
links it as `~/.local/bin/meertex-gui`, writes `meertex.desktop` with the icon, and
makes it the handler for `meertex://` links (`xdg-mime`). Desktop sign-in needs
that last step. `make distinstall MEERTEX_URL=http://localhost:8060` bakes
another server into the desktop entry. On macOS it copies `meertex.app` to
`~/Applications` and links the same `meertex-gui` command.

The command is `meertex-gui` and not `meertex`, because `meertex` is the CLI
(`make cli` in the repo root), and both live in `~/.local/bin`. The `.deb` and
`.rpm` install `/usr/bin/meertex-gui` for the same reason. The app menu, the
window and the `meertex://` links still say meertex. An older install that
linked the app as `~/.local/bin/meertex` loses that link on the next
`make distinstall`, and `make cli` puts the CLI back there.

## Releases

`.github/workflows/desktop.yml` builds the app on GitHub's macOS, Windows and
Linux runners on every push to `main` that changes `electron/`, `VERSION` or the
workflow, and on demand under Actions. It publishes one GitHub release per run,
`desktop-<VERSION>.<run number>`, with these files.

| file | for |
| --- | --- |
| `meertex-mac-arm64.dmg` | macOS on Apple silicon |
| `meertex-mac-x64.dmg` | macOS on Intel |
| `meertex-windows-setup.exe` | Windows (installer) |
| `meertex-linux-amd64.deb` | Debian, Ubuntu |
| `meertex-linux-x86_64.rpm` | Fedora (built in CI only, it needs `rpmbuild`) |
| `meertex-linux-x86_64.AppImage` | any Linux, without installing |

The landing page links each one as `releases/latest/download/<file>`.
`tests/test_landing.py` checks that its links and the workflow's `FILES` agree.
The app reports `VERSION` as its version and the run number as its build
number.

Nothing is signed with a paid certificate. The macOS build is ad-hoc signed
(`-c.mac.identity=-`), so macOS stops the first start and lets the user allow
it under System Settings, Privacy & Security, "Open Anyway". Windows shows
SmartScreen once ("More info", "Run anyway"). Notarization needs an Apple
Developer ID, and a quiet Windows install a code signing certificate. Both go
in as repository secrets ([electron.build/code-signing](https://www.electron.build/code-signing)).

Dependabot (`.github/dependabot.yml`) opens a pull request when Electron or
electron-builder has a new version. Merging it changes `electron/`, so the
workflow publishes a new release.

## How sign-in works

meertex signs you in with an emailed link and a code (meerpad's flow). In a
desktop window the normal `https://.../api/auth/callback` link would open your
**system browser**, and the session cookie would land there instead of here.
So the shell does this.

1. The page sees `window.meertexDesktop` (from `preload.js`) and asks for its
   sign-in email with `client: "desktop"` (`app/static/js/login.js`).
2. The server mails a `meertex://login?token=...` link instead of the web one
   (`app/routers/auth.py`).
3. Clicking it starts or focuses this app, which loads the real callback URL
   **inside its window**, so the cookie is stored here.

The code in the same email always works too. Type it into the window. That is
the way in when the mail is read on another device, where a `meertex://` link
has no app to go to, and with `npm start`, where no installed desktop entry
claims the scheme.

Only `meertex://login?token=...` is acted on, and only to drive that callback.
A link that arrives while the app is closed is kept and used once the window
exists. On Linux and Windows it comes in on the command line, on macOS as an
`open-url` event.

## What else the shell adds over a browser tab

- **A window of its own**, with the app's icon in the dock and the task bar.
- **Back and Forward** (Alt+Left/Right, Cmd+[ and Cmd+] on macOS), since there
  is no browser toolbar. The page sees a key first, so inside the editor these
  keys keep the editor's meaning, as in a browser. Home is Ctrl+Shift+H.
- **PDFs, compile logs and raw files** open in a window of their own inside the
  app, where the session cookie is. Chromium's PDF viewer shows the PDFs.
  DOIs, conference pages and every other site open in the system browser.
- **Unsent edits are not lost silently.** When the editor still has edits the
  server has not seen (only while offline), closing the window, Reload or Home
  asks first, as a browser does. Electron would otherwise just keep the window
  open without a word.
- **Spelling suggestions and Cut/Copy/Paste on right-click**, which Electron
  does not show by default. The editor's own checker is the server's
  LanguageTool. The browser's spellchecker only runs where the page turns it
  on, and it checks `en-US` and `de-DE` (`MEERTEX_SPELLCHECK_LANGS=en-GB,fr`
  to change that).
- **Permissions** (the clipboard for the copy buttons) only for the server's own
  pages.
- **The agent panel** in the editor, below.

## The agent panel

The editor's Agent button shows a chat with the Claude Code of this computer,
or with an Ollama model that Claude Code talks to, as a fourth column or under
the text (`docs/DESIGN.md` §20, and the paper's Agent tab for the setup). The
agent works in the paper's checkout (`meertex checkout`), and `meertex sync`
brings its edits into the editor.

- `agent.js` runs the `claude` command with stream-json on stdin and stdout
  and answers its permission prompts with native dialogs. `MEERTEX_CLAUDE`
  names another command, `OLLAMA_HOST` another Ollama.
- `panel/` is the chat. It is served from `meertex-agent://panel` into an
  iframe of the editor, so the server's page cannot script it.
  `preload.js` gives that frame the chat's bridge and the server's pages only
  the context, the status and the panel's moves.
- The chat lives in the main process, one per paper, until the app quits. The
  provider and the models are in `agent.json` in the app's user data folder.

To try it against a local server, check a paper of that server out with the
CLI pointed at it, run `meertex sync`, and start the app with
`MEERTEX_URL=http://localhost:8060 npm start`. A second instance next to an
installed app needs its own `--user-data-dir`, or the installed one takes the
start over. Started from a Claude Code terminal, the app keeps that session's
variables away from the agent.

The app icon is `build/icon.png` (1024x1024), scaled up from the 512 px logo in
`app/static/img/logo-512.png`. A larger original would make it sharper. Code
signing and notarization are not configured. Add your certificates for
distributable macOS builds.
