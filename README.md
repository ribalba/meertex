# meertex

<img src="app/static/img/logo-192.png" alt="" width="96" align="right">

A meerkat LaTeX editor for the whole life of a paper, from the call for papers
to camera-ready. Every paper carries its conference, its deadlines with
countdowns, and fixed versions of what was handed in. You write locally, in VS
Code or with Claude, and a small sync client keeps the folder in step with the
server within a second. Coauthors see and edit the current state instead of an
outdated copy.

- **Live sync of a local folder.** `meertex sync` runs in the background and
  merges your edits with everyone else's, character by character (a CRDT, Yjs
  format). A git history is kept on the server as snapshots, and
  `git clone https://meertex.com/p/<name>.git` copies it for a backup
  (read-only, the API token is the password). For a big rewrite,
  `meertex hold` keeps your changes local while coauthors' edits still arrive,
  and `meertex release` sends them in one go.
- **Papers, not just files.** Conference, track, links, page limit, template,
  and every date from abstract registration to camera-ready, in the venue's
  time zone (AoE by default).
- **It asks when a date passes.** "Did you hand it in?" makes the fixed
  submission version from the state at the deadline. "Was it accepted?" sets
  the outcome, so `meertex projects --outcome accepted` lists all your
  accepted papers.
- **Versions.** Submission, rebuttal, Revision N, camera-ready, as git tags
  with the PDF you handed in. Browse, download and diff them.
- **Folders** for your own order, and a naming pattern (default
  `{year}-{conference}-{paper}`, e.g. `2027-icse-green-llm`).
- **An API and a CLI for agents.** Claude creates projects from a conference
  URL with the `meertex-new-paper` skill (`skills/`), and reads deadlines,
  versions and diffs through `meertex --json`.
- **Edit in the browser** with coauthors at the same time, cursors and
  names included, on the same document your local folder syncs with. LaTeX
  autocomplete, a PDF that compiles as you type, compile errors on their
  lines, and SyncTeX between PDF and source.
- **Reviews like GitHub's.** Comment on spans of text or on the PDF, suggest
  changes, resolve with the diff of how a request was addressed, accept or
  reopen. `meertex threads`, `reply` and `resolve` let Claude work through them.
- **Plans and a timeline.** A venue you want to submit to before the topic is
  clear, and a Gantt chart of every paper with its writing window (your
  writing time before the deadline) and where they overlap.
- **Reminders before deadlines.** One mail a day, 14, 3 and 1 days before a
  deadline and on the day (your choice), in the venue's time zone, with when
  to start writing and the questions waiting. Mute it per paper.
  `meertex reminders` shows what the next one says.
- **Publisher templates.** Start a paper from an author kit's zip.
- **A reference check.** Every `.bib` entry is looked up at doi.org,
  Crossref, Semantic Scholar, DBLP, arXiv and Open Library, and its links are
  fetched. Made-up citations, DOIs of other papers, wrong authors, dead links
  and retracted papers show up in a References tab and in the editor. Claude
  can check whether a source supports the sentence that cites it. Mark an
  entry you checked by hand, and the mark breaks when the entry changes.
  `meertex refs` does the same from the shell.
- **Spelling and grammar while you write.** The same self-hosted LanguageTool
  meerail uses underlines mistakes in the editor. Commands, math and citations
  are skipped, so a correct paper stays quiet. Your own dictionary and the
  rules you turned off are in Settings. Nothing leaves your server.

[docs/DESIGN.md](docs/DESIGN.md) is the contract for all of it.

## Use it

[meertex.com](https://meertex.com) runs meertex for everyone. Sign in with your
email address and write in the browser. For a local folder, install the CLI
(Python 3.11 or newer).

```bash
pipx install meertex                 # or: uv tool install meertex
meertex login --server meertex.com
meertex new --title "My paper" --conference ICSE --year 2027 --date submission=2027-01-15@AoE --checkout
meertex sync                         # or: meertex service install (Linux and macOS)
```

The CLI is on [PyPI](https://pypi.org/project/meertex/). Every push to `main`
that changes it publishes a new version, so `pipx upgrade meertex` brings the
newest one.

## Run your own server

You need Docker with Compose. The server and Postgres need about 1 GB of memory,
compiling adds up to 2 GB, the spelling checker (LanguageTool) up to 1.5 GB, and
the TeX Live image takes about 6 GB of disk.

```bash
make up              # Postgres + server -> http://127.0.0.1:8060
make logs            # the sign-in code is printed here without SMTP
make cli             # pipx install ./cli
meertex login --server http://127.0.0.1:8060
meertex new --title "My paper" --conference ICSE --year 2027 --date submission=2026-07-31@AoE --checkout
meertex sync         # or: meertex service install
```

On a server, Coolify deploys `docker-compose.coolify.yml` behind TLS. Set
`BASE_URL` and SMTP, give the web service its domain and deploy.
[COOLIFY.md](COOLIFY.md) has every step.

Development: `make venv`, `make dev` (native server with reload), `make compile-dev`
(the compile service with your TeX Live), `make test-db`, `make test`, `make lint`.

## Releasing the CLI

`.github/workflows/cli.yml` runs on every push to `main` that touches `cli/`,
`VERSION` or the workflow itself, and on demand under Actions. It runs the CLI's
tests on Python 3.11 and 3.13, builds the package with the version `VERSION`
plus the run number (`0.1.0.42`, a normal release that pip sorts above `0.1.0`)
and publishes it to PyPI. A push that changes only the server publishes nothing,
because the package would be the same.

Publishing uses PyPI's trusted publishing, so there is no token to keep. It
needs a publisher set up once on pypi.org, under Your account, Publishing,
"Add a new pending publisher": PyPI project name `meertex`, owner `ribalba`,
repository `meertex`, workflow `cli.yml` and environment `pypi`. The first run
then creates the project. `make cli` still installs the CLI from `./cli` for
development.

## Layout

| | |
| --- | --- |
| `app/` | The server (FastAPI). `app/live/` is the live document, its rooms, the sync endpoint and the git history. `app/references/` is the reference check, `app/grammar/` the spelling and grammar check. |
| `app/static/` | The web app (plain JavaScript, no build step). The editor's libraries are one vendored module in `app/static/vendor/editor/`. |
| `tools/editor/` | Builds that module (`make editor`), and the editor's browser tests. |
| `compile/` | The compile service: TeX Live in a container of its own, behind a small HTTP API ([compile/README.md](compile/README.md)). |
| `cli/` | The `meertex` command and its sync client. |
| `skills/` | The Claude Code skill that starts a paper from a CFP (`make install-skill`). |
| `docs/DESIGN.md` | The contract every part codes against. |
| `docs/agent.md` | How an agent uses meertex. |

## License

AGPL-3.0-or-later.
