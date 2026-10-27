# The browser editor's bundle and its tests

`app/static/vendor/editor/editor.js` is the one module the editor page
(`app/static/js/pages/editor.js`) loads. It holds yjs, y-websocket,
y-codemirror.next and CodeMirror 6 with the LaTeX mode and autocomplete.
`app/static/vendor/pdfjs/` holds PDF.js and its worker, which
`app/static/js/pdfview.js` loads to show PDFs. The web app has no build step,
so both are built here and committed. Pages load them with a dynamic
`import()`, so no other page pays for them.

## Building the bundle

```sh
make editor
```

This runs `npm ci` and `node build.mjs` in this folder and writes `editor.js`
and `LICENSES.txt` (the licenses of every bundled package) next to each other.
It also copies `pdf.js`, `pdf.worker.js` and their license from pdfjs-dist's
legacy build, which carries polyfills for browsers that are not the newest.
The build stops when a package would end up in the bundle twice. Two copies of
yjs or of `@codemirror/state` break the editor in ways that are hard to see.

`entry.js` lists everything the page takes from the bundle. Add an export
there when the page needs something new.

To update a package, pick a version that was published at least two weeks ago
(`npm view <package> time --json`), write it into `package.json` without `^`
or `~`, run `npm install` here so `package-lock.json` follows, then
`make editor` and the tests below. `overrides` in `package.json` holds
`@lezer/common`, a dependency of CodeMirror, at an older release for the same
reason.

## Tests

Two scripts. Neither is part of the server's pytest suite.

`tests/test_livefiles.py` checks that the browser's copy of the file list
rules (`app/static/js/livefiles.js`) agrees with `app/live/ydoc.py` on 400
generated documents, path clashes and malformed entries included. It needs
node and the repository's virtualenv:

```sh
.venv/bin/python tools/editor/tests/test_livefiles.py
```

`tests/test_editor.py` drives Google Chrome through Playwright against a real
server, a real database and the real `meertex sync`. It starts its own server
on port 8065 with a temporary `DATA_DIR`, makes four accounts with sessions
right in the database, creates the papers through the API, and stops
everything when it is done. The CLI runs with `MEERTEX_HOME`,
`MEERTEX_PAPERS` and `XDG_CONFIG_HOME` in the temporary folder, so your own
checkouts and configuration are never touched.

It needs these things once:

1. The repository's virtualenv with the CLI (`make venv`).
2. Postgres from `make infra` and a database of its own. The server makes
   the tables when it starts.

   ```sh
   docker compose exec -T db psql -U meertex -d postgres -c "CREATE DATABASE meertex_editor"
   ```

3. A virtualenv with Playwright and httpx. Google Chrome must be installed,
   because the tests use it instead of Playwright's own Chromium.

   ```sh
   python3 -m venv ~/.cache/meertex-pw
   ~/.cache/meertex-pw/bin/pip install playwright httpx
   ```

Then run it from the repository's root:

```sh
~/.cache/meertex-pw/bin/python tools/editor/tests/test_editor.py --compile --shots /tmp/editor-shots
```

It prints one line per test and exits with 1 when one failed. A full run takes
about two minutes, because one test keeps an editor open and idle for 90
seconds while the others run.

| Option | |
| --- | --- |
| `--shots DIR` | Save screenshots at 1280 and 390 pixels into DIR. |
| `--no-idle` | Skip the 90 second idle test. |
| `--port N` | The port of the server the script starts (8065). |
| `--db URL` | The database (`MEERTEX_EDITOR_DB`, or `meertex_editor` on port 5436). |
| `--server URL` | Use a server that is already running on `--db` instead. The offline test is skipped then. |
| `--tmp DIR` | Where the server's data and the CLI checkout go. |
| `--compile` | Start the compile service (`compile/service.py`) natively with this machine's TeX Live, on port 8075 (`--compile-port`). Without a compile service the PDF tests are skipped. |
| `--compile-url URL`, `--compile-token T` | Or use a compile service that runs already (`COMPILE_URL`, `COMPILE_TOKEN`). |
| `--headed` | Show the browser windows. |
| `NAME ...` | Run only these tests. |

What the tests cover:

- Typing reaches the server within a second, umlauts and emoji included.
  Ctrl+S, search and undo.
- Two browsers type into the same line at once and end up with the same
  text. Each one sees the other's cursor with a name, and who is here.
- An edit through `PUT /files` shows up in an open editor.
- A `meertex checkout` with `meertex sync` running: a browser edit lands in
  the local file and a local edit lands in the browser.
- The file tree: new file, new folder, rename, a rename by someone else while
  the file is open, upload through the picker and by dropping a file, moving a
  file by dragging it onto a folder, delete, and a delete by someone else.
- A viewer cannot type.
- Back and Forward between files on one socket. Leaving the page closes the
  socket and stops every interval the editor started.
- The spellcheck toggle is remembered, and the tree folds away at phone width.
- An empty paper starts with "New file", and the Overview and Files tabs lead
  into the editor.
- A path clash shows the second file as a conflict copy, like the server.
- An idle editor keeps one connection for 90 seconds.
- The server goes away and comes back, and what was typed meanwhile arrives.
- Autocomplete: labels after `\ref{` and `\cref{` from every .tex file
  (commented ones left out, new ones without a reload), keys after
  `\citep{` with their titles from a .bib file, paths after
  `\includegraphics{` and `\input{`, `\begin{` with its body and `\end`,
  and the paper's own commands.

With a compile service (`--compile`, which needs latexmk, synctex and
latexdiff on the `PATH`) also:

- The PDF pane compiles when the editor opens and two seconds after typing,
  shows the page count, and lists an error, whose `file:line` opens its line.
  On a phone the PDF takes the place of the text and goes back.
- SyncTeX both ways: Ctrl+Shift+Enter marks the cursor's line in the PDF, and
  a double-click there leads back to it.
- A version's compiled PDF on the Versions tab with its preview, and the PDF
  diff of the Compare view.

## Reviews

`tests/test_reviews_ui.py` drives the review pages (`pages/reviews.js`,
`pages/review.js`, `pages/review-files.js`, `threads.js`) and, in the
editor, the comments (`editor-comments.js`), the compile's problems on their
lines (`editor-problems.js`) and the place a double-click in the PDF points at
(`editor-spot.js`). It works like `test_editor.py`: its own server on port
8066 with a temporary `DATA_DIR`, accounts and sessions made in the database,
papers made through the API. It needs a database of its own, made once:

```sh
docker compose exec -T db psql -U meertex -d postgres -c "CREATE DATABASE meertex_reviews_ui"
```

```sh
~/.cache/meertex-pw/bin/python tools/editor/tests/test_reviews_ui.py --compile --shots /tmp/review-shots
```

The tests build on each other in order. `NAME ...` runs the named tests and
those before them, and `--just NAME ...` only the named ones, for the tests at
the end that make a paper of their own. `--keep` keeps the temporary folder
with the server's log. The other options are those of `test_editor.py`, with
the compile service on port 8076.

What the tests cover:

- A reviewer starts a review on a version, comments on a selection with
  umlauts and an emoji, suggests a change, comments on a line and a range of
  lines from their numbers, and submits with a verdict. Nobody else sees the
  pending review or its threads.
- An editor replies, applies the suggestion and resolves another thread with
  a note. The reviewer sees how the text changed as a word diff, accepts one
  and reopens the other, and edits a comment.
- A viewer cannot comment. A thread whose text was deleted shows as outdated,
  and `/reviews#thread-<id>` marks it.
- Back and Forward between Conversation, Files and Changes, and every page at
  390 pixels without a horizontal scroll.
- With a compile service: the PDF next to the review's source, a thread marks
  its lines in it, a double-click opens the comment box at the line, and the
  latexdiff PDF of the changes.
- In the editor: highlights of the open threads that stay on their words while
  typing, the comments panel in the PDF's column, a comment on a selection
  after an emoji (UTF-16 and code points differ), resolving there, a reviewer's
  pending review that only she sees, and a viewer who reads only.
- With a compile service: errors and warnings on their lines (gutter icon, red
  or amber line, the message on hover), right after an edit and after another
  file was open, gone after the fix and a new compile, and shown to viewers.
  A double-click on a word in the PDF selects it in the text, the clicked one
  of two in a long line, `na\"ive` for the PDF's "naïve", and the highlight
  fades.

## The timeline and planned papers

`tests/test_timeline.py` drives the timeline (`pages/timeline.js`), planned
papers on the new-paper form (`pages/new.js`), the list (`pages/home.js`) and
the overview (`pages/overview.js`), and the writing time in the settings
(`pages/account.js`). It works like `test_editor.py`: its own server on port
8069 with a temporary `DATA_DIR`, accounts and sessions made in the database,
papers made through the API with dates counted from today. It needs a
database of its own, made once:

```sh
docker compose exec -T db psql -U meertex -d postgres -c "CREATE DATABASE meertex_timeline"
```

```sh
~/.cache/meertex-pw/bin/python tools/editor/tests/test_timeline.py --shots /tmp/timeline-shots
```

The tests build on each other in order, and a run takes about fifteen
seconds. The script stops when something listens on its port already. Pick
another with `--port`. `--keep` keeps the temporary folder with the server's
log. The other options are those of `test_editor.py`.

What the tests cover:

- An empty timeline says how to add a paper or a plan, and the List and
  Timeline tabs lead to each other.
- A marker's hover card and its keyboard focus show the venue's day: 23:59
  AoE on one day is not shown as the next day. A name in the sticky column
  takes focus without scrolling the chart, and a marker far to the right is
  scrolled into sight.
- The writing load strip shows 2 where two writing windows overlap, with
  both papers on hover, per day at the Weeks zoom. The zoom is remembered.
- "I need N days" moves the writing bars at once, saves, and stays after a
  reload. The overview says when to start writing, and the settings have the
  same field.
- A paper's own writing time from its row's menu, and back to the usual time
  when the field is emptied.
- A plan made without a title is marked Planned in the list, filters with
  Planned, and is drawn dashed on the timeline. Starting it gives it a title,
  a new name and the basic files.
- At 390 pixels only the chart scrolls sideways, under a fixed name column.
- Finished papers are hidden until "Show finished papers", which is
  remembered, and the folder filter takes subfolders along.
- Share makes a public link. A browser without a session sees the chart with
  its zoom and hover cards, and no links into the papers, row menus, writing
  time field, folder filter or header menu, at 1440 and at 390 pixels. A
  folder narrows the link to that folder and its subfolders. "New link" ends
  the old link and "Stop sharing" ends the new one.

## References

`tests/test_references_ui.py` drives the References tab
(`pages/references.js`), its counter in the paper's tabs and the reference
check's errors and warnings in the editor (`pages/editor-pdf.js`). It works
like `test_editor.py`: its own server on port 8067 with a temporary
`DATA_DIR`, accounts and sessions made in the database, papers made through
the API. The server runs with `REFERENCES_NETWORK=false` and without
`ANTHROPIC_API_KEY`, so nothing goes to the internet. What a check run would
have found is written into `reference_results` directly. The check buttons, a
run's progress and adding an entry need a server with lookups, so those tests
change the report in the browser with Playwright's routes and answer the
check's routes there. It needs a database of its own, made once:

```sh
docker compose exec -T db psql -U meertex -d postgres -c "CREATE DATABASE meertex_references_ui"
```

```sh
~/.cache/meertex-pw/bin/python tools/editor/tests/test_references_ui.py --shots /tmp/reference-shots
```

The tests build on each other in order, and a run takes about fifteen
seconds. `NAME ...` runs the named tests and those before them. `--keep`
keeps the temporary folder with the server's log, and `--port`, `--db`
(`MEERTEX_REFERENCES_UI_DB`), `--tmp` and `--headed` work as in
`test_editor.py`.

What the tests cover:

- The tab sits between Reviews and Settings, and its counter shows the
  entries that are error or changed plus the broken ones, in amber.
- The header's counts per status and the last run, by whom and when. With
  lookups off, editors read that instead of the check buttons.
- The status filter and the search over keys, titles and authors, kept in
  the query string, and "Clear the filter" when nothing matches.
- A row opens with the mouse and the keyboard, to its findings with links to
  the record, the entry next to the record, and a claim with its verdict,
  sentence, passage and line.
- "Take this value" writes the record's title into `refs.bib`. "Mark as
  checked" and "Remove the mark" on a key with `:` and `/`, and an entry that
  changed after it was marked counts as a problem.
- The broken entries and the missing keys with links to their lines, which
  open the editor at that line (`/edit/<path>#L<n>`).
- A viewer reads and changes nothing.
- With lookups played by the browser: a 503 as a flash, the claims' note on
  cost, a run's progress bar asked for every 1.5 seconds, and the report
  read again when it ends. "Add a reference" previews the BibTeX, shows a
  404 and a 503 in the dialog, and adds.
- The editor lists the reference check's errors and warnings under the PDF,
  marked References, and marks their lines, also without a compile service.
- At 390 pixels without a horizontal scroll, and the empty states: no .bib
  file, no entries and nothing checked yet.
