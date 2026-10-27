# meertex-compile

The compile service of meertex ([docs/DESIGN.md §14](../docs/DESIGN.md)). The
meertex server never runs TeX. It sends the files of a project here as a
tar.gz, gets back a status and the log, and fetches the PDF. The job's folder
stays here for a while, so SyncTeX can map between the PDF and the sources.

`service.py` is the whole service. It uses only Python's standard library, so
it runs the same in its Docker image and natively next to a TeX Live
installation.

## Running it

**In Docker**, as part of the stack. `docker-compose.yml` (laptop) and
`docker-compose.coolify.yml` (Coolify) build it from this folder and point
`web` at `http://compile:8000`.

```bash
docker compose build compile     # the first build pulls 2.6 GB of TeX Live
docker compose up -d compile
```

**Natively**, with this machine's TeX Live (latexmk, synctex and latexdiff on
the `PATH`), for `make dev`.

```bash
make compile-dev                 # :8070, the token from .env's COMPILE_TOKEN
```

Or by hand.

```bash
COMPILE_TOKEN=secret COMPILE_PORT=8070 python3 -I compile/service.py
```

The tests start it the same way (`tests/compile_helpers.py`) and skip when
latexmk is missing.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `COMPILE_TOKEN` | (required) | Every request but `/healthz` carries `Authorization: Bearer <token>`. The service does not start without one. |
| `COMPILE_HOST`, `COMPILE_PORT` | `127.0.0.1`, `8000` | The image listens on `0.0.0.0:8000`. |
| `COMPILE_JOBS_DIR` | `<temp>/meertex-compile-jobs` | One folder per job. `/jobs` in the image, a volume. |
| `COMPILE_TIMEOUT_SECONDS` | 90 | Wall clock time per job, latexdiff included. |
| `COMPILE_KEEP_JOBS` | 30 | The newest finished jobs are kept for SyncTeX, older ones are removed. |
| `COMPILE_WORKERS` | 2 | Jobs that run at once. A job that finds no free worker within the timeout gets 503. |
| `COMPILE_MAX_UPLOAD_BYTES` | 256 MB | The largest tar.gz. |
| `COMPILE_MAX_TREE_BYTES` | 512 MB | The largest unpacked total. |
| `COMPILE_MAX_FILES` | 20000 | Entries in one upload. |
| `COMPILE_MAX_FILE_BYTES` | 256 MB | The largest file a job may write (`RLIMIT_FSIZE`, Linux). |

## API

Errors are `{detail}`.

| Route | |
| --- | --- |
| `GET /healthz` | `{ok: true}`, without a token. |
| `POST /jobs?main_file=main.tex&compiler=pdflatex` | The body is a tar.gz of the project. Returns `{job_id, status, duration_ms, log}`. `status` is `success` (a PDF without errors), `errors` (a PDF, but TeX or latexmk reported errors), `failure` (no PDF) or `timeout`. 400 when the upload is refused, 503 when no worker became free. |
| `POST /latexdiff?main_file=...&compiler=...` | The tar.gz holds two trees, `old/...` and `new/...`. `latexdiff --flatten` of the two main files replaces the new one, which is then compiled like a job. |
| `GET /jobs/{id}/output.pdf`, `GET /jobs/{id}/output.log` | 404 when the job is unknown, was removed, or made no PDF. |
| `POST /jobs/{id}/synctex/edit {page, x, y}` | `{file, line, column}`, with `file` relative to the project. 422 when SyncTeX knows nothing there. |
| `POST /jobs/{id}/synctex/view {file, line, column}` | `[{page, x, y, width, height}]`. |

Coordinates are PDF points from the top left of the page. `column` is 0 when
SyncTeX does not know it, which is most of the time.

The log is TeX's log of the main file, run with `max_print_line=10000` so no
message or path is wrapped. When a job does not succeed, latexmk's last lines
follow after the line `=== latexmk (the last lines of its output) ===`.

## How a job is boxed in

- The upload is unpacked by the service's own rules: only regular files and
  folders, no absolute paths, no `..`, no links or device files, nothing
  twice, at most `COMPILE_MAX_FILES` entries and `COMPILE_MAX_TREE_BYTES`. The
  decompressed stream is capped too, so a small gzip bomb stops early.
- latexmk runs with `-norc`, because a `latexmkrc` in the project would be
  Perl that runs here. TeX runs with `-no-shell-escape`, `-interaction=nonstopmode`,
  `-file-line-error` and `-synctex=1`, in the folder of the main file.
- kpathsea runs in paranoid mode (`openin_any=p`, `openout_any=p`): a
  document reads and writes only below its folder. LuaTeX 1.22 (TeX Live 2025)
  applies these rules to Lua's `io.open` as well. luaotfload opens TeX Live's
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
- A job's environment has none of the `COMPILE_*` settings, the token
  included.
- The image adds an unprivileged user, and the compose files a read-only root
  file system, a tmpfs for `HOME` and the font caches, no capabilities,
  `no-new-privileges`, an init that reaps killed processes, and memory, CPU
  and process limits. The tmpfs allows `exec`, because TeX Live's biber is a
  packed program that unpacks its Perl into `/tmp` and runs it from there.

On Coolify the container can reach the internet (see COOLIFY.md). Nothing in
a job opens a connection, though. TeX has no shell escape, and the biber guard
refuses URLs.

## The image

`texlive/texlive:TL2025-historic`, the full scheme of TeX Live 2025, pinned by
digest in the Dockerfile. It is about 2.6 GB to download and 5.5 GB on disk.
It brings Python 3, latexmk, synctex, latexdiff, biber and a prebuilt
luaotfload font database. To move to a newer TeX Live, change the tag and
the digest together (`docker buildx imagetools inspect texlive/texlive:<tag>`),
then run `make test` natively and one compile through the container.
