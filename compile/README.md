# meertex-compile

The compile service of meertex ([docs/DESIGN.md §14](../docs/DESIGN.md)). The
meertex server never runs TeX. It sends the files of a project here as a
tar.gz, gets back a status and the log, and fetches the PDF. The job's folder
stays here for a while, so SyncTeX can map between the PDF and the sources.

`service.py`, `sandbox.py` and `markdown.lua` are the whole service. It uses
only Python's standard library, so it runs the same in its Docker image and
natively next to a TeX Live installation. A Markdown main file needs pandoc
as well. `sandbox.py` is the box every job runs in
([The sandbox](#the-sandbox)).

## Running it

**In Docker**, as part of the stack. `docker-compose.yml` (laptop) and
`docker-compose.coolify.yml` (Coolify) build it from this folder and point
`web` at `http://compile:8000`.

```bash
docker compose build compile     # the first build pulls 2.6 GB of TeX Live
docker compose up -d compile
```

**Natively**, with this machine's TeX Live (latexmk, synctex and latexdiff on
the `PATH`, and pandoc for Markdown), for `make dev`. Without pandoc, a
Markdown main file fails with a message that says so.

```bash
make compile-dev                 # :8070, the token from .env's COMPILE_TOKEN
```

Or by hand.

```bash
COMPILE_TOKEN=secret COMPILE_PORT=8070 python3 -I compile/service.py
```

The tests start it the same way (`tests/compile_helpers.py`) and skip when
latexmk is missing, the Markdown tests also when pandoc is missing. The tests
of the sandbox skip on a kernel without Landlock.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `COMPILE_TOKEN` | (required) | Every request but `/healthz` carries `Authorization: Bearer <token>`. The service does not start without one. |
| `COMPILE_HOST`, `COMPILE_PORT` | `127.0.0.1`, `8000` | The image listens on `0.0.0.0:8000`. |
| `COMPILE_JOBS_DIR` | `<temp>/meertex-compile-jobs` | One folder per job. `/jobs` in the image, a volume. |
| `COMPILE_TIMEOUT_SECONDS` | 90 | Wall clock time per job, latexdiff included. |
| `COMPILE_KEEP_JOBS` | 30 | The newest finished jobs are kept for SyncTeX, older ones are removed. Every workspace keeps that many of its own as well. |
| `COMPILE_KEEP_WORKSPACES` | 20 | The workspaces used last are kept. A paper whose workspace was removed is sent whole the next time. |
| `COMPILE_WORKERS` | 2 | Jobs that run at once. A job that finds no free worker within the timeout gets 503. |
| `COMPILE_MAX_UPLOAD_BYTES` | 256 MB | The largest tar.gz. |
| `COMPILE_MAX_TREE_BYTES` | 512 MB | The largest unpacked total. |
| `COMPILE_MAX_FILES` | 20000 | Entries in one upload. |
| `COMPILE_MAX_FILE_BYTES` | 256 MB | The largest file a job may write (`RLIMIT_FSIZE`, Linux). |
| `COMPILE_MAX_HOME_BYTES` | 512 MB | The most a workspace's `HOME` keeps from one compile to the next. Larger caches are removed after the compile and made again by the next one. |
| `COMPILE_SANDBOX` | `auto` | [The sandbox](#the-sandbox) every job runs in. `auto` uses it where the kernel has Landlock (Linux 5.13 and later) and warns at the start where it has not. `on` refuses to start without it. `off` never uses it. The first line the service prints says which it is. |

## API

Errors are `{detail}`.

| Route | |
| --- | --- |
| `GET /healthz` | `{ok: true}`, without a token. |
| `POST /jobs?main_file=main.tex&compiler=pdflatex` | The body is a tar.gz of the project. Returns `{job_id, status, duration_ms, log}`. `status` is `success` (a PDF without errors), `errors` (a PDF, but TeX or latexmk reported errors), `failure` (no PDF) or `timeout`. 400 when the upload is refused, 503 when no worker became free. |
| `POST /latexdiff?main_file=...&compiler=...` | The tar.gz holds two trees, `old/...` and `new/...`. `latexdiff --flatten` of the two main files replaces the new one, which is then compiled like a job. |
| `POST /workspaces/{id}/prepare {files: {path: sha256}, clean?}` | Gets a paper's workspace ready and returns `{need: [path], token}`, the files that have to be uploaded. Everything that is not one of `files` is removed. `clean` forgets the build files. 400 for a path or hash it does not take. |
| `POST /jobs?main_file=...&compiler=...&workspace=...&token=...` | Compiles in the workspace after `prepare`. The body is a tar.gz of the files in `need`. Also answers `status: cancelled` without a `job_id`. 409 when the token is gone, and the caller prepares again. |
| `POST /workspaces/{id}/cancel` | Kills the job that runs in the workspace. Returns `{cancelled}`, false when none runs. |
| `POST /workspaces/{id}/forget` | Removes the workspace and its jobs, for a paper that was deleted. Returns `{forgotten: true}`. |
| `GET /jobs/{id}/output.pdf`, `GET /jobs/{id}/output.log` | 404 when the job is unknown, was removed, or made no PDF. |
| `POST /jobs/{id}/synctex/edit {page, x, y}` | `{file, line, column}`, with `file` relative to the project. 422 when SyncTeX knows nothing there. |
| `POST /jobs/{id}/synctex/view {file, line, column}` | `[{page, x, y, width, height}]`. |

Coordinates are PDF points from the top left of the page. `column` is 0 when
SyncTeX does not know it, which is most of the time.

The log is TeX's log of the main file, run with `max_print_line=10000` so no
message or path is wrapped. When a job does not succeed, latexmk's last lines
follow after the line `=== latexmk (the last lines of its output) ===`.

## Markdown

A main file that ends in `.md` or `.markdown` is turned into LaTeX first, and
TeX compiles that. pandoc reads it as CommonMark with pandoc's extensions
(`commonmark_x`: tables, footnotes, math in `$`, fenced divs, attributes, raw
LaTeX in `` `...`{=latex} `` and ` ```{=latex} `), plus GitHub's bare links and
math, and a lone image with alt text as a figure. It writes
`<main file>.tex` next to the Markdown with its standalone LaTeX template.
The front matter sets the template's variables (`title`, `author`, `date`,
`abstract`, `documentclass`, `fontsize`, `header-includes`, ...). `geometry`
defaults to `margin=2.5cm` and `colorlinks` to true.

pandoc reads with `sourcepos`, which gives every element its place in the
Markdown. `markdown.lua` turns these places into comments that TeX never sees:
`%meertex:N` before every block, and at the end of every line of a paragraph
the line the next one comes from. The service reads them back into a line map
(`markdown-lines.json` in the job), so that

- `synctex/edit` answers with the Markdown file and its line,
- `synctex/view` takes the Markdown file and its line,
- TeX's errors (`./notes.md.tex:42: ...`) name the Markdown file and its line.
  Warnings stay with the LaTeX, which is not a file of the paper.

pandoc runs with `--sandbox` (it reads only the Markdown file) and a data
folder that does not exist, so only its own templates. A document cannot name
a filter or a template. `/latexdiff` turns both sides into LaTeX, without the
marks, and diffs those.

## Workspaces

An open editor compiles at every pause in typing, so its compiles run in the
paper's workspace (`<jobs>/workspaces/<id>`) instead of a fresh folder. The
paper's files stay in `tree/`, and only changed ones are sent. Before each
compile everything else in the tree is removed, and the build files of the
last run that reached its end come back from `seeds/`, outside TeX's reach. A
small edit then usually takes one TeX pass instead of three, and a run that
was killed halfway leaves nothing the next one could trip over. A run that
used those files and made no PDF, or reported an error inside one of them,
runs once more without them.

All compiles of a workspace belong to one paper. A file a document changed is
noticed by its size and time and uploaded again. Versions, diffs and the CLI
always run in a fresh folder.

The workspace also holds the `HOME` of the paper's compiles (`home/`, see
[The sandbox](#the-sandbox)). `clean` removes it with the build files.

## The sandbox

Every program of a job (latexmk and what it starts, pandoc, latexdiff) runs
inside `sandbox.py`, which closes the machine to it with
[Landlock](https://docs.kernel.org/userspace-api/landlock.html) before the
program starts. Landlock is a rule of the kernel's. A process gives up access
for itself and for everything it starts, it cannot get it back, and it needs
no privilege to do so. The rules are on the files themselves, so a link, a
`..` or another working directory lead nowhere new.

| A job | |
| --- | --- |
| reads and runs | TeX Live's trees, the system's programs and settings (`/usr`, `/etc` and the like), Python, and this folder. |
| reads and writes | The paper's files (the job's `tree/`, and `old/` for a diff), its `HOME` and its `TMPDIR`. |
| runs programs of its own from | Its `HOME` only, because biber unpacks itself there. |
| cannot open | Anything else. That is `/proc`, where the environment of the service and of the container can be read, another paper's workspace or job, the service's own files and `/tmp`. |
| cannot make | Links, sockets, pipes and device files. |
| cannot connect | Over TCP, from Landlock version 4 on (Linux 6.7). |

This is what a paper's privacy rests on. The rules in the next section bind
single programs, and one of them has a hole. kpathsea's paranoid mode keeps
TeX below its folder and LuaTeX applies it to `io.open`, but `io.input` and
`io.output` open any path and `kpse.lookup` finds files anywhere. Without the
sandbox a lualatex document reads and overwrites every file the service's
user can, other papers and the container's environment among them. On Coolify
that environment holds every secret of the resource, because Coolify hands
every variable to every service.

Each job has a `HOME` and a `TMPDIR` of its own. `HOME` holds caches with code
that the next run loads (LuaTeX's font caches, and the 120 MB of Perl that
biber unpacks of itself), so two papers never share one. A workspace keeps its
`HOME` (`<jobs>/workspaces/<id>/home`), so only a paper's first compile pays
for the caches, about 3 seconds when it uses biber. A job without a workspace
gets a fresh one that goes when the job ends.

`COMPILE_SANDBOX` is `auto` by default. The service then uses Landlock where
the kernel has it and says so in the first line it prints
(`jobs in a sandbox (Landlock 4)`). On a kernel without it the line says
`jobs WITHOUT a sandbox`, a warning follows, and compiling works as it did
before there was a sandbox. `on` makes the service refuse to start instead.
Docker's default seccomp profile lets Landlock through, and no capability is
needed. macOS has no Landlock, so `make compile-dev` on a Mac runs without the
sandbox.

Landlock does not limit `stat`, so a job can still learn whether a path
exists. It cannot list a folder it was not given.

## How a job is boxed in

- Every program of a job runs in [the sandbox](#the-sandbox).
- The upload is unpacked by the service's own rules: only regular files and
  folders, no absolute paths, no `..`, no links or device files, nothing
  twice, at most `COMPILE_MAX_FILES` entries and `COMPILE_MAX_TREE_BYTES`. The
  decompressed stream is capped too, so a small gzip bomb stops early.
- latexmk runs with `-norc`, because a `latexmkrc` in the project would be
  Perl that runs here. TeX runs with `-no-shell-escape`, `-interaction=nonstopmode`,
  `-file-line-error` and `-synctex=1`, in the folder of the main file.
- kpathsea runs in paranoid mode (`openin_any=p`, `openout_any=p`): TeX
  reads and writes only below its folder. LuaTeX 1.22 (TeX Live 2025)
  applies these rules to Lua's `io.open` as well, but not to `io.input` and
  `io.output`, which is why the sandbox is there. luaotfload opens TeX Live's
  own files by absolute path, so lualatex runs with `TEXMFOUTPUT` set to TeX
  Live's root, the one other place paranoid mode reads from. latexmk sets
  `TEXMFOUTPUT` to `.` for every run, so the lualatex command sets it itself.
- latexdiff is Perl and kpathsea does not bind it. Before it runs, a tree
  whose `.tex` files `\input` an absolute path or a path with `..` is refused.
- biber is Perl too, and it reads any path and fetches URLs. latexmk runs it
  through `service.py --biber-guard`, which reads the data sources from the
  `.bcf` (where TeX has expanded every macro) and stops when one is
  absolute, has `..`, starts with `~` or is a URL. biber then runs with
  `--noconf`, so a `biber.conf` in the project is not read. The refusal is a
  line starting with `meertex-compile: ` in the log, which the server lists as
  an error.
- The main file's name holds only letters, digits, spaces and `. , + = @ -`,
  because latexmk passes it to a shell in double quotes, where a backtick
  would run a command.
- Every job runs in a process group of its own, which is killed when the
  time is up. A file a job writes is capped at `COMPILE_MAX_FILE_BYTES`, core
  dumps are off.
- A job's environment holds `PATH`, the locale, TeX's settings and its own
  `HOME` and `TMPDIR`. Nothing else of the service's or the container's
  environment reaches it, so neither the token nor a password the container
  holds. LuaTeX gives a document `os.getenv`.
- The image adds an unprivileged user, and the compose files a read-only root
  file system, a tmpfs for the service's own `HOME`, no capabilities,
  `no-new-privileges`, an init that reaps killed processes, and memory, CPU
  and process limits.

On Coolify the container can reach the internet (see COOLIFY.md). A job
cannot open a TCP connection where the kernel's Landlock is version 4 or
later. On an older kernel nothing in a job opens one either. TeX has no shell
escape and LuaTeX no socket library without it, and the biber guard refuses
URLs.

## The image

`texlive/texlive:TL2025-historic`, the full scheme of TeX Live 2025, pinned by
digest in the Dockerfile. It is about 2.6 GB to download and 5.5 GB on disk.
It brings Python 3, latexmk, synctex, latexdiff, biber and a prebuilt
luaotfload font database. To move to a newer TeX Live, change the tag and
the digest together (`docker buildx imagetools inspect texlive/texlive:<tag>`),
then run `make test` natively and one compile through the container.

The image comes without the format of xelatex. TeX would build it on a job's
first run into the job's `HOME`, which takes seven seconds, and every job has
a `HOME` of its own. So the Dockerfile builds it once for all of them
(`fmtutil-sys --byfmt xelatex`).

pandoc is its static release build from GitHub (about 170 MB), pinned by
`PANDOC_VERSION` and the archive's SHA-256 for amd64 and arm64. CI installs
the same one. To move to a newer pandoc, change the version and both
checksums in the Dockerfile and in `.github/workflows/tests.yml` (`gh api
repos/jgm/pandoc/releases/tags/<version>` lists each asset's `digest`).
