"""meertex-compile: the compile service (docs/DESIGN.md §14).

The meertex server never runs TeX. It sends the files of a project here as a
tar.gz and gets back a status and the log. The job's folder stays here for a
while, so the PDF can be fetched and SyncTeX can map between the PDF and the
sources. This file, the box it runs every job in (sandbox.py) and its Lua
filter for Markdown (markdown.lua) are the whole service. It uses only the
standard library, so it runs the same in its Docker image
(compile/Dockerfile) and natively next to a developer's TeX Live
(``make compile-dev``).

A Markdown main file (``.md``) is turned into LaTeX first, by pandoc with
markdown.lua, and TeX compiles that LaTeX (``<main file>.tex`` next to it).
The filter leaves the Markdown's line numbers in the LaTeX as comments, and
SyncTeX's lines and TeX's errors are mapped back through them, so the editor
never sees the LaTeX.

A document is untrusted input. TeX reads and writes files, and LaTeX packages
run Lua, so every job is boxed in:

* A fresh folder per job. The upload is unpacked by our own rules: no absolute
  paths, no ``..``, no links or device files, and a cap on the total size.
* Every program of a job runs in a box of the kernel's (Landlock, sandbox.py).
  It reads TeX Live and the system's programs, it writes to the paper's
  files, a HOME and a TMPDIR of its own, and nothing else on the machine is
  open to it, neither /proc, where the container's environment is, nor
  another paper's folder. It cannot open a TCP connection. The rules below
  bind single programs, and the Lua of LuaTeX gets around kpathsea's
  (``io.input`` opens any path), so they are no longer what a paper's
  privacy rests on.
* latexmk without rc files (``-norc``, because a latexmkrc is Perl), TeX
  without shell escape.
* kpathsea's paranoid mode (``openin_any=p``, ``openout_any=p``): TeX reads
  and writes only below the job's folder. lualatex may also read TeX Live's
  own tree (see ``latexmk_engine``).
* A wall clock limit, after which the job's whole process group is killed,
  and a cap on the size of any one file a job writes.
* biber is Perl, so kpathsea's rules do not bind it. latexmk runs it through
  ``biber_guard``, which refuses bibliographies outside the job's folder or
  on the network before biber reads them.
* pandoc runs with ``--sandbox``, so it reads nothing but the Markdown file,
  and with a data folder that does not exist, so no template or filter of
  anyone else's. A document cannot name a filter or a template.
* A job's environment holds a handful of variables (``tex_env``) and none of
  the service's or the container's own. LuaTeX gives a document
  ``os.getenv``, and on Coolify every container of a resource holds every
  secret of the resource.

The container adds the rest: an unprivileged user, a read-only root file
system, and memory, CPU and process limits (docker-compose*.yml).

A compile from an open editor has to be quick, so it runs in the paper's
workspace instead of a fresh folder (``Workspace``). The paper's files stay
there between compiles and only changed ones are sent. The build files of the
last run that reached its end are put back, so a small edit usually needs one
TeX pass instead of three. Its HOME stays as well, with the font caches.
Nothing else survives from one compile to the next, and nothing at all goes
from one paper to another. Versions, diffs and the CLI never use a workspace.
"""

import contextlib
import gzip
import hmac
import html
import io
import json
import logging
import math
import os
import re
import resource
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("meertex-compile")

# The compilers the server offers (§14, the project's `compiler`).
COMPILERS = ("pdflatex", "xelatex", "lualatex")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# latexmk hands the main file's name to a shell inside double quotes, where
# a backtick or a $ still runs a command. So its name holds letters, digits,
# spaces and a few safe signs, and starts with a letter or digit (never "-").
MAIN_FILE_PART_RE = re.compile(r"^\w[\w .,+=@-]*$")
JOB_PATH_RE = re.compile(r"^/jobs/([0-9a-f]{32})/(output\.pdf|output\.log|synctex/edit|synctex/view)$")
# A workspace is named by the server, with the project's id.
WORKSPACE_RE = re.compile(r"^[0-9a-f-]{1,64}$")
WORKSPACE_PATH_RE = re.compile(r"^/workspaces/([0-9a-f-]{1,64})/(prepare|cancel|forget)$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# The log of a job that did not succeed ends with latexmk's last lines after
# this line. The server's problem parser stops reading the TeX log here.
LATEXMK_MARK = "=== latexmk (the last lines of its output) ==="
LATEXMK_TAIL_LINES = 60
# Lines the service's own guards print. The server lists them as errors.
GUARD_PREFIX = "meertex-compile: "
# What a job's log may hold in a response. The start of a longer log is cut.
MAX_LOG_CHARS = 4 * 1024 * 1024
MAX_JSON_BODY = 64 * 1024
# The list of a paper's files and their hashes.
MAX_MANIFEST_BODY = 32 * 1024 * 1024
# What one run may hand to the next. A run that writes more starts the next
# one from nothing, which is only slower.
MAX_SEED_BYTES = 64 * 1024 * 1024
MAX_SEED_FILES = 2000
SYNCTEX_SECONDS = 15
# Arguments of \input and friends, checked before latexdiff --flatten reads
# them, because latexdiff is Perl and kpathsea's rules do not bind it.
INCLUDE_RE = re.compile(
    r"\\(?:input|include|includeonly|subfile|import|subimport|inputfrom|includefrom|subinputfrom|subincludefrom)"
    r"\*?\s*\{([^}]*)\}(?:\s*\{([^}]*)\})?"
    r"|\\input\s+([^\s{}\\%]+)"
)
# A main file with one of these endings is Markdown.
MARKDOWN_SUFFIXES = (".md", ".markdown")
# CommonMark with pandoc's extensions (tables, footnotes, math in $, fenced
# divs, attributes, raw LaTeX in ```{=latex}), plus what GitHub adds: bare
# links and its math. A lone image with a caption is a figure.
MARKDOWN_FROM = "commonmark_x+implicit_figures+autolink_bare_uris+tex_math_gfm"
MARKDOWN_FILTER = Path(__file__).resolve().with_name("markdown.lua")
# The marks markdown.lua leaves in the LaTeX, at the end of a line or on a
# line of their own. A % of the text is \% in the LaTeX.
MARKDOWN_MARK_RE = re.compile(r"(?<!\\)%meertex:(\d+)\s*$")
# What a Markdown job keeps: the Markdown line of every line of the LaTeX.
MARKDOWN_LINES = "markdown-lines.json"
# The launcher that puts a job's program into its box (``Sandbox``).
SANDBOX = Path(__file__).resolve().with_name("sandbox.py")
SANDBOX_MODES = ("auto", "on", "off")
# The only variables of the service's own environment that a job gets, plus
# the locale's (LC_*). Everything else stays here. On Coolify every container
# of a resource holds every variable of the resource, the passwords of the
# database and the mail server among them, and LuaTeX gives a document
# os.getenv.
ENV_KEEP = ("PATH", "LANG", "LANGUAGE", "TZ")
# Where the programs, their libraries and their settings are. A job reads
# these, and TeX Live's own trees wherever they are (``Sandbox``).
SYSTEM_ROOTS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt",
    "/var/lib/texmf", "/var/cache/fontconfig",
)  # fmt: skip
# A program of a job that is installed somewhere else is read from there.
SANDBOX_TOOLS = ("latexmk", "pdflatex", "xelatex", "lualatex", "bibtex", "biber", "latexdiff", "pandoc", "kpsewhich", "perl")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Config:
    token: str
    host: str
    port: int
    jobs_dir: Path
    timeout: int
    keep_jobs: int
    keep_workspaces: int
    workers: int
    max_upload: int
    max_tree: int
    max_files: int
    max_file: int
    max_home: int
    sandbox: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("COMPILE_TOKEN", "").strip()
        if not token:
            sys.exit("COMPILE_TOKEN is not set. The service refuses to run without a token.")
        jobs = os.environ.get("COMPILE_JOBS_DIR", "").strip()
        sandbox = os.environ.get("COMPILE_SANDBOX", "").strip().lower() or "auto"
        if sandbox not in SANDBOX_MODES:
            sys.exit(f"COMPILE_SANDBOX is one of {', '.join(SANDBOX_MODES)}, not {sandbox}.")
        return cls(
            token=token,
            host=os.environ.get("COMPILE_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_env_int("COMPILE_PORT", 8000),
            jobs_dir=Path(jobs) if jobs else Path(tempfile.gettempdir()) / "meertex-compile-jobs",
            timeout=_env_int("COMPILE_TIMEOUT_SECONDS", 90),
            keep_jobs=_env_int("COMPILE_KEEP_JOBS", 30),
            keep_workspaces=_env_int("COMPILE_KEEP_WORKSPACES", 20),
            workers=_env_int("COMPILE_WORKERS", 2),
            max_upload=_env_int("COMPILE_MAX_UPLOAD_BYTES", 256 * 1024 * 1024),
            max_tree=_env_int("COMPILE_MAX_TREE_BYTES", 512 * 1024 * 1024),
            max_files=_env_int("COMPILE_MAX_FILES", 20000),
            max_file=_env_int("COMPILE_MAX_FILE_BYTES", 256 * 1024 * 1024),
            max_home=_env_int("COMPILE_MAX_HOME_BYTES", 512 * 1024 * 1024),
            sandbox=sandbox,
        )


class Refused(Exception):
    """An upload or a request that is not accepted. The message says why."""


class Busy(Exception):
    """Every worker stayed busy for as long as a compile may take."""


class UnknownJob(Exception):
    """A job that never existed or was removed to make room."""


class NoPdf(Exception):
    """A job that made no PDF, so SyncTeX has nothing to work on."""


class Stale(Exception):
    """A workspace job whose preparation is gone, after a restart for example.
    The caller prepares again."""


# --- Paths and uploads ------------------------------------------------------------


def clean_path(name: str) -> str | None:
    """``name`` as a relative POSIX path without ``.``, ``..`` or empty
    segments, or None when it is absolute, climbs out or is empty."""
    if not name or "\\" in name or "\0" in name or name.startswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


class _Capped:
    """A stream that refuses to give more than ``limit`` bytes. It caps the
    decompressed size of an upload, so a small gzip bomb cannot fill memory or
    disk before the tar headers are even looked at."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self.raw = raw
        self.limit = limit
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.count += len(data)
        if self.count > self.limit:
            raise Refused(f"The upload unpacks to more than {self.limit} bytes")
        return data


def unpack(body: bytes, roots: dict[str, Path], cfg: Config) -> None:
    """Unpack a tar.gz. ``roots`` maps a first path segment to the folder it
    goes to. The key "" takes every path as it is. Only regular files and
    folders are accepted, below their root, at most ``max_files`` of them
    and ``max_tree`` bytes in all."""
    count = 0
    total = 0
    try:
        raw = gzip.GzipFile(fileobj=io.BytesIO(body))
        # The tar format adds headers and padding to the files' bytes.
        stream = _Capped(raw, cfg.max_tree + 64 * 1024 * 1024)
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                count += 1
                if count > cfg.max_files:
                    raise Refused(f"The upload has more than {cfg.max_files} entries")
                name = member.name
                if name.startswith("/"):
                    raise Refused(f"{name} is an absolute path")
                if name in (".", "./"):
                    continue
                rel = clean_path(name)
                if rel is None:
                    raise Refused(f"{name} is not a usable path. Absolute paths and '..' are refused.")
                if not (member.isdir() or member.isreg()):
                    raise Refused(f"{name} is a link or a device file. Only files and folders are accepted.")
                root, parts = _place(rel, roots)
                if not parts:
                    continue
                target = root.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += member.size
                if total > cfg.max_tree:
                    raise Refused(f"The files are larger than {cfg.max_tree} bytes in all")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise Refused(f"{name} cannot be read")
                # O_EXCL refuses a path that is in the upload twice, and
                # O_NOFOLLOW never writes through a link.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(source, out)
    except FileExistsError as e:
        raise Refused(f"{e.filename} is in the upload twice, or is a file and a folder at once") from None
    except NotADirectoryError as e:
        raise Refused(f"{e.filename} is a file and a folder at once") from None
    except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
        raise Refused(f"The upload is not a usable tar.gz ({e})") from None


def _place(rel: str, roots: dict[str, Path]) -> tuple[Path, list[str]]:
    parts = rel.split("/")
    if "" in roots:
        return roots[""], parts
    if parts[0] not in roots:
        raise Refused(f"{rel} is outside {', '.join(sorted(r + '/' for r in roots))}")
    return roots[parts[0]], parts[1:]


def is_markdown(main_file: str) -> bool:
    return main_file.lower().endswith(MARKDOWN_SUFFIXES)


def tex_name(main_file: str) -> str:
    """The file TeX compiles for ``main_file``: the file itself, or for a
    Markdown file the LaTeX pandoc makes of it, next to it."""
    return f"{main_file}.tex" if is_markdown(main_file) else main_file


def markdown_lines(tex: str) -> list[int]:
    """The Markdown line of every line of the LaTeX that pandoc made with
    markdown.lua, and 0 for a line without text of the document: the
    preamble, empty lines and the marks' own lines. A line takes the line of
    the mark before it. Lines of the body before the first mark (the title of
    the front matter) take line 1."""
    lines: list[int] = []
    current = 1
    body = False
    for text in tex.split("\n"):
        if not body:
            body = text.strip() == "\\begin{document}"
            lines.append(0)
            continue
        mark = MARKDOWN_MARK_RE.search(text)
        if mark is not None and text.lstrip().startswith("%meertex:"):
            current = int(mark[1])
            lines.append(0)
            continue
        lines.append(current if text.strip() else 0)
        if mark is not None:
            current = int(mark[1])
    return lines


def markdown_line(lines: list[int], tex_line: int) -> int:
    """The Markdown line of a line of the LaTeX. A line without one takes the
    nearest one before it, or after it."""
    if not lines:
        return 1
    at = min(max(tex_line, 1), len(lines)) - 1
    for k in (*range(at, -1, -1), *range(at + 1, len(lines))):
        if lines[k]:
            return lines[k]
    return 1


def latex_line(lines: list[int], md_line: int) -> int:
    """The first line of the LaTeX that shows the Markdown line ``md_line``,
    or the nearest line before it that the LaTeX shows (an empty line, the
    front matter). A footnote's text is where the footnote mark is."""
    best, at = 0, 0
    for k, line in enumerate(lines):
        if best < line <= md_line:
            best, at = line, k + 1
    if at:
        return at
    return next((k + 1 for k, line in enumerate(lines) if line), 1)


def map_markdown_log(log: str, main_file: str, lines: list[int]) -> str:
    """TeX's errors name the LaTeX and its lines (-file-line-error). They are
    made to name the Markdown file and its lines, so the editor marks them
    there. Warnings name the file only by opening it, and stay with the
    LaTeX, which is not a file of the paper."""
    md = Path(main_file).name
    pattern = re.compile(rf"^((?:\./)?){re.escape(Path(tex_name(main_file)).name)}:(\d+): ", re.MULTILINE)
    return pattern.sub(lambda m: f"{m[1]}{md}:{markdown_line(lines, int(m[2]))}: ", log)


def _own_message(message: str) -> str:
    """A message of the service at the end of a log, which the server lists
    as an error (app/texlog.py)."""
    return f"\n{LATEXMK_MARK}\n{GUARD_PREFIX}{message}\n"


def check_includes(tree: Path) -> None:
    """Refuse a tree whose .tex files \\input something outside of it, before
    latexdiff --flatten would read and paste that file into the diff. TeX
    itself is held by openin_any, latexdiff is Perl and is not."""
    for path in sorted(tree.rglob("*.tex")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in INCLUDE_RE.finditer(text):
            for group in match.groups():
                for arg in (group or "").split(","):
                    arg = arg.strip()
                    if arg.startswith(("/", "~")) or ".." in arg.replace("\\", "/").split("/"):
                        raise Refused(f"{path.relative_to(tree)} includes {arg}, which is outside the project")


# --- Running TeX ------------------------------------------------------------------


def texmf_root() -> str:
    """TeX Live's root folder (``kpsewhich -var-value TEXMFROOT``)."""
    try:
        out = subprocess.run(
            ["kpsewhich", "-var-value", "TEXMFROOT"], capture_output=True, text=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError) as e:
        sys.exit(f"kpsewhich does not work ({e}). Is TeX Live installed and on the PATH?")
    root = out.stdout.strip()
    if not root:
        sys.exit("kpsewhich does not know TEXMFROOT. Is TeX Live installed?")
    return root


def texmf_trees() -> list[str]:
    """The folders TeX Live's own files are in, the fonts and formats this
    machine made included. A tree in a user's home is not one of them."""
    names = ("TEXMFROOT", "TEXMFDIST", "TEXMFLOCAL", "TEXMFSYSVAR", "TEXMFSYSCONFIG")
    try:
        out = subprocess.run(
            ["kpsewhich", "-expand-var", "|".join(f"${name}" for name in names)],
            capture_output=True, text=True, timeout=30, check=True,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return []
    # A variable kpathsea does not know stays as it was written.
    return [tree for tree in out.stdout.strip().split("|") if os.path.isabs(tree)]


# The data sources biblatex hands biber, in the .bcf file it writes for it.
BCF_SOURCE_RE = re.compile(r"<bcf:datasource\b[^>]*>([^<]*)</bcf:datasource>")
URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def biber_guard(args: list[str]) -> int:
    """Run biber with ``args`` if every bibliography in the .bcf is a file
    inside the job's folder. A path from the document can name any file, or
    a URL, and biber would read or fetch it: kpathsea's paranoid mode binds
    TeX and bibtex, but biber is Perl. The .bcf holds the paths after TeX has
    expanded every macro, so checking it is enough. Also no biber.conf from
    the project (``--noconf``). latexmk runs this as its biber command."""
    name = args[-1] if args else ""
    bcf = Path(name if name.endswith(".bcf") else f"{name}.bcf")
    try:
        text = bcf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""  # biber says what is missing
    for raw in BCF_SOURCE_RE.findall(text):
        path = html.unescape(raw).strip()
        if URL_RE.match(path) or path.startswith("~") or clean_path(path) is None:
            print(f"{GUARD_PREFIX}biber does not read {path}, which is outside the project.", file=sys.stderr)
            return 2
    os.execvp("biber", ["biber", "--noconf", *args])
    return 2  # not reached


def latexmk_biber() -> list[str]:
    """latexmk's switch that runs biber through ``biber_guard``."""
    command = f"{shlex.quote(sys.executable)} -I {shlex.quote(os.path.abspath(__file__))} --biber-guard %O %S"
    perl = command.replace("\\", "\\\\").replace("'", "\\'")
    return ["-e", f"$biber = '{perl}';"]


def latexmk_engine(compiler: str, texmf: str) -> list[str]:
    """latexmk's switches for a compiler.

    lualatex runs with TEXMFOUTPUT set to TeX Live's root. In paranoid mode
    kpathsea refuses absolute paths outside TEXMFOUTPUT, and LuaTeX 1.22 (TeX
    Live 2025) applies that to Lua's io.open too. luaotfload opens TeX Live's
    own files by absolute path, so without it lualatex stops before the first
    page. Writing there stays impossible, the tree belongs to root. It is set
    in the command because latexmk sets TEXMFOUTPUT to its aux folder for
    every run. The command is ours, a document cannot change it (-norc)."""
    if compiler == "pdflatex":
        return ["-pdf"]
    if compiler == "xelatex":
        return ["-xelatex"]
    return ["-lualatex", f"-lualatex=env TEXMFOUTPUT={shlex.quote(texmf)} lualatex %O %S"]


def tex_env() -> dict[str, str]:
    """The environment of every TeX, latexmk, latexdiff, pandoc and synctex
    run. It holds the few variables of the service's own that a job needs
    (ENV_KEEP) and TeX's settings. A job gets a HOME and a TMPDIR of its own
    on top (``Service._run``). None of the service's settings is in it, and
    nothing else the container was started with."""
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP or k.startswith("LC_")}
    env.update(
        openin_any="p",
        openout_any="p",
        shell_escape="f",
        # One message per line. The server finds files and line numbers in
        # the log, and TeX's default wraps at 79 characters, in mid-path.
        max_print_line="10000",
        error_line="254",
        half_error_line="238",
    )
    return env


def _limit(pid: int, max_file: int) -> None:
    """Cap the size of any one file the job writes (an endless \\write fills a
    disk) and switch core dumps off. Set from outside right after the start,
    because a preexec_fn is not safe in a threaded server. Children of the
    process inherit the limits."""
    if not hasattr(resource, "prlimit"):  # Linux only
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, ValueError, OSError):
        resource.prlimit(pid, resource.RLIMIT_FSIZE, (max_file, max_file))
        resource.prlimit(pid, resource.RLIMIT_CORE, (0, 0))


@dataclass(frozen=True)
class Box:
    """What one job may write to. ``home`` is its HOME, where LuaTeX keeps
    its font caches and biber unpacks itself, and the only place it runs
    programs of its own from. ``tmp`` is its TMPDIR. ``folders`` hold the
    paper's files."""

    home: Path
    tmp: Path
    folders: tuple[Path, ...]


class Sandbox:
    """Runs a job's programs in the box of compile/sandbox.py. That is
    Landlock, the kernel's own rules about which files a process and
    everything it starts may open. A job reads TeX Live and the system's
    programs (``read``) and writes to what its ``Box`` names. The rest of
    the machine is closed to it, /proc with the container's environment and
    the other papers' folders included, and so is TCP.

    ``mode`` is COMPILE_SANDBOX. ``auto`` uses Landlock where the kernel has
    it (Linux 5.13 and later), ``on`` refuses to start without it, ``off``
    never uses it. Without it a job is held only by the rules of its
    programs, which the Lua of LuaTeX gets around."""

    def __init__(self, mode: str, jobs_dir: Path, path: str | None) -> None:
        self.mode = mode
        self.abi = 0 if mode == "off" else self._abi()
        if mode == "on" and not self.on:
            sys.exit("COMPILE_SANDBOX is on, but this kernel has no Landlock (Linux 5.13 and later have it).")
        self.read = self._read_roots(jobs_dir, path) if self.on else []

    @property
    def on(self) -> bool:
        return self.abi >= 1

    def __str__(self) -> str:
        if self.on:
            return f"jobs in a sandbox (Landlock {self.abi})"
        return "jobs WITHOUT a sandbox (" + ("COMPILE_SANDBOX is off" if self.mode == "off" else "this kernel has no Landlock") + ")"

    @staticmethod
    def _abi() -> int:
        try:
            out = subprocess.run(
                [sys.executable, "-I", str(SANDBOX), "--abi"], capture_output=True, text=True, timeout=30, check=True
            )
            return int(out.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0

    @staticmethod
    def _read_roots(jobs_dir: Path, path: str | None) -> list[str]:
        """The folders every job may read and run programs from."""
        candidates = [
            *SYSTEM_ROOTS,
            *texmf_trees(),
            # Python itself, for the biber guard.
            sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
            # This file, which is the biber guard, and markdown.lua.
            str(SANDBOX.parent),
        ]  # fmt: skip
        for tool in SANDBOX_TOOLS:
            found = shutil.which(tool, path=path)
            if found:
                candidates.append(os.path.dirname(os.path.realpath(found)))
        jobs = os.path.realpath(jobs_dir)
        roots: list[str] = []
        for candidate in candidates:
            root = os.path.realpath(candidate)
            if not os.path.isabs(candidate) or not os.path.exists(root):
                continue
            if any(root == known or root.startswith(known + os.sep) for known in roots):
                continue
            if root == os.sep or jobs == root or jobs.startswith(root + os.sep):
                # Reading it would open every paper's folder to every job.
                logger.warning("The jobs are kept below %s, so no job gets to read that folder.", root)
                continue
            roots.append(root)
        return roots

    def command(self, cmd: list[str], box: Box) -> list[str]:
        """``cmd`` as it runs inside ``box``."""
        if not self.on:
            return cmd
        flags: list[str] = []
        for root in self.read:
            flags += ["-r", root]
        for folder in (*box.folders, box.tmp):
            flags += ["-w", str(folder)]
        flags += ["-x", str(box.home)]
        return [sys.executable, "-I", str(SANDBOX), *flags, "--", *cmd]


class Runner:
    """Starts commands in process groups of their own and kills a group when
    its time is up. Remembers the running ones, so a shutdown kills them too."""

    def __init__(self, max_file: int) -> None:
        self.max_file = max_file
        self.running: set[subprocess.Popen] = set()
        self.lock = threading.Lock()

    def run(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        stdout: Path,
        stderr: Path | None,
        deadline: float,
        on_start: Callable[[subprocess.Popen], None] | None = None,
    ) -> tuple[int | None, bool]:
        """``(exit code, timed out)``. Output goes to files, never to pipes, so
        a chatty process cannot block on a full pipe. ``on_start`` gets the
        process, so another thread can stop it."""
        with stdout.open("wb") as out, (stderr.open("wb") if stderr else contextlib.nullcontext()) as err:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err if err is not None else subprocess.STDOUT,
                start_new_session=True,
            )
        _limit(proc.pid, self.max_file)
        with self.lock:
            self.running.add(proc)
        if on_start is not None:
            on_start(proc)
        try:
            try:
                return proc.wait(timeout=max(0.1, deadline - time.monotonic())), False
            except subprocess.TimeoutExpired:
                # The leader is not reaped yet, so its id still names this group.
                self.kill(proc)
                proc.wait()
                return None, True
        finally:
            with self.lock:
                self.running.discard(proc)

    @staticmethod
    def kill(proc: subprocess.Popen) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    def kill_all(self) -> None:
        with self.lock:
            procs = list(self.running)
        for proc in procs:
            self.kill(proc)


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _tail_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


# An error TeX switched off (\ignoreprimitiveerror, which the LaTeX kernel of
# TeX Live 2025 uses when a longtable breaks across pages) is logged as
# "./main.tex:12: ignored error: ..." and is no error (app/texlog.py).
_ERROR_RE = re.compile(r"^(?:! |\S[^\n]*?\.\w+:\d+: (?!ignored error: ))", re.MULTILINE)
# An error TeX reports inside a file an earlier run wrote (-file-line-error).
_BUILD_FILE_ERROR_RE = re.compile(r"^[^\n:]+\.(?:aux|bbl|toc|lof|lot|out|nav|snm|vrb|ind|gls|acr|nls):\d+: ", re.MULTILINE)


def _seeds_to_blame(status: str, log: str) -> bool:
    """Whether a run that started from an earlier run's files should be done
    again without them. Half typed text gives errors all the time, and those
    are the text's own. A run that made no PDF, or an error inside a build
    file, may come from what the earlier run left."""
    return status == "failure" or (status == "errors" and _BUILD_FILE_ERROR_RE.search(log) is not None)


# --- Workspaces -------------------------------------------------------------------


@dataclass
class _Pending:
    """What a preparation found, for the job that comes next."""

    token: str
    files: dict[str, str]
    need: list[str]


class Workspace:
    """The folder one paper's editor compiles share.

    ``tree`` holds the paper's files, which stay so that only changed ones are
    sent. Everything else in it is swept away before every compile. What the
    compile should find again comes from ``seeds``, the build files of the last
    run that reached its end. A run that was stopped halfway leaves a cut off
    .aux behind, and none of that can reach the next run this way.

    All compiles of a workspace belong to one paper, so its writers are the
    same people the paper's text already trusts. A file a document changed is
    noticed by its size and time and sent again.

    ``home`` is the HOME of the paper's compiles. The font caches of LuaTeX
    and what biber unpacks of itself stay there, so only a paper's first
    compile pays for them. No other paper's compile reads or writes it,
    because these caches hold code that the next run loads."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.tree = folder / "tree"
        self.seeds = folder / "seeds"
        self.home = folder / "home"
        self.state = folder / "state.json"
        # One preparation or job at a time.
        self.lock = threading.Lock()
        self.pending: _Pending | None = None
        # {path: {sha, size, mtime_ns}}, None until read from state.json.
        self.files: dict[str, dict] | None = None
        # The main file and compiler the seeds were made with.
        self.seeds_for: dict[str, str] | None = None
        # What cancel, from another thread, looks at.
        self.guard = threading.Lock()
        self.running = False
        self.cancelled = False
        self.proc: subprocess.Popen | None = None

    # --- State ---

    def load(self) -> None:
        self.tree.mkdir(parents=True, exist_ok=True)
        os.utime(self.folder)  # the workspaces used longest ago are removed first
        if self.files is not None:
            return
        try:
            saved = json.loads(self.state.read_text())
            self.files, self.seeds_for = dict(saved["files"]), saved.get("seeds_for")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.files, self.seeds_for = {}, None

    def save(self) -> None:
        tmp = self.state.with_suffix(".tmp")
        tmp.write_text(json.dumps({"files": self.files, "seeds_for": self.seeds_for}))
        os.replace(tmp, self.state)

    def forget(self) -> None:
        """After a failed upload nobody knows what the tree holds. The next
        preparation asks for every file."""
        self.files = {}
        self.save()

    # --- The paper's files ---

    def sweep(self, keep: Iterable[str]) -> None:
        """Remove everything in the tree that is not one of the files in
        ``keep``, without following links."""
        files = set(keep)
        folders = {path.rsplit("/", i)[0] for path in files for i in range(1, path.count("/") + 1)}
        for folder, dirs, names in os.walk(self.tree):
            rel = os.path.relpath(folder, self.tree).replace(os.sep, "/")
            prefix = "" if rel == "." else f"{rel}/"
            for name in list(dirs):
                full = os.path.join(folder, name)
                if os.path.islink(full):
                    os.unlink(full)
                    dirs.remove(name)
                elif prefix + name not in folders:
                    shutil.rmtree(full, ignore_errors=True)
                    dirs.remove(name)
            for name in names:
                if prefix + name not in files:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(os.path.join(folder, name))

    def intact(self, path: str, sha: str) -> bool:
        """Whether the tree holds the content ``sha`` at ``path``."""
        known = (self.files or {}).get(path)
        if known is None or known["sha"] != sha:
            return False
        try:
            st = os.lstat(self.tree / path)
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size == known["size"] and st.st_mtime_ns == known["mtime_ns"]

    def apply(self, body: bytes, pending: _Pending, cfg: Config) -> None:
        """Put the uploaded files in place and remember what the tree holds."""
        try:
            for path in pending.need:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.tree / path)
            unpack(body, {"": self.tree}, cfg)
            files: dict[str, dict] = {}
            total = 0
            for path, sha in pending.files.items():
                try:
                    st = os.lstat(self.tree / path)
                except OSError:
                    raise Refused(f"{path} did not come with the upload") from None
                files[path] = {"sha": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
                total += st.st_size
            if total > cfg.max_tree:
                raise Refused(f"The files are larger than {cfg.max_tree} bytes in all")
        except Refused:
            self.forget()
            raise
        self.files = files
        self.save()

    # --- What one run hands to the next ---

    def restore_seeds(self, main_file: str, compiler: str) -> bool:
        """Put the last good run's build files into the tree. A paper's own
        file with the same name wins. False when there were none to use."""
        if self.seeds_for != {"main_file": main_file, "compiler": compiler} or not self.seeds.is_dir():
            return False
        keep = set(self.files or {})
        found = False
        for folder, _dirs, names in os.walk(self.seeds):
            for name in names:
                source = Path(folder) / name
                rel = source.relative_to(self.seeds).as_posix()
                if rel in keep:
                    continue
                target = self.tree / rel
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                except OSError:
                    continue  # a paper's file is where this one's folder would be
                found = True
        return found

    def save_seeds(self, main_file: str, compiler: str, skip: set[str]) -> None:
        """Keep what the run wrote, for the next one. Only a run that got to its end comes here."""
        new = self.folder / "seeds.new"
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir()
        keep = set(self.files or {})
        count, total = 0, 0
        for folder, _dirs, names in os.walk(self.tree):
            for name in names:
                source = os.path.join(folder, name)
                rel = os.path.relpath(source, self.tree).replace(os.sep, "/")
                if rel in keep or rel in skip:
                    continue
                st = os.lstat(source)
                if not stat.S_ISREG(st.st_mode):
                    continue
                count, total = count + 1, total + st.st_size
                if count > MAX_SEED_FILES or total > MAX_SEED_BYTES:
                    shutil.rmtree(new, ignore_errors=True)
                    self.drop_seeds()
                    return
                target = new / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        shutil.rmtree(self.seeds, ignore_errors=True)
        os.replace(new, self.seeds)
        self.seeds_for = {"main_file": main_file, "compiler": compiler}
        self.save()

    def drop_seeds(self) -> None:
        shutil.rmtree(self.seeds, ignore_errors=True)
        self.seeds_for = None
        self.save()

    def trim_home(self, limit: int) -> None:
        """Drop the caches in ``home`` once they are larger than ``limit``.
        The next run makes them again. They stay from one compile to the
        next, so without this a paper could fill the disk a little at a time."""
        total = 0
        for folder, _dirs, names in os.walk(self.home):
            for name in names:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(folder, name)).st_size
            if total > limit:
                shutil.rmtree(self.home, ignore_errors=True)
                return

    # --- Stopping the job that runs ---

    def begin(self) -> None:
        with self.guard:
            self.running, self.cancelled, self.proc = True, False, None

    def end(self) -> None:
        with self.guard:
            self.running, self.cancelled, self.proc = False, False, None

    def started(self, proc: subprocess.Popen) -> None:
        with self.guard:
            self.proc = proc
            stop = self.cancelled
        if stop:
            Runner.kill(proc)

    def ended(self) -> None:
        with self.guard:
            self.proc = None

    def stopped(self) -> bool:
        with self.guard:
            return self.cancelled

    def cancel(self) -> bool:
        """Stop the job that runs here. False when there is none."""
        with self.guard:
            if not self.running:
                return False
            self.cancelled = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            Runner.kill(proc)
        return True


# --- The service ------------------------------------------------------------------


class Service:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.texmf = texmf_root()
        self.env = tex_env()
        self.runner = Runner(cfg.max_file)
        self.slots = threading.BoundedSemaphore(cfg.workers)
        self.lock = threading.Lock()
        # Jobs being made. Pruning leaves them alone.
        self.active: set[str] = set()
        self.workspaces_dir = cfg.jobs_dir / "workspaces"
        self.workspaces: dict[str, Workspace] = {}
        cfg.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspaces_dir.mkdir(exist_ok=True, mode=0o700)
        self.sandbox = Sandbox(cfg.sandbox, cfg.jobs_dir, self.env.get("PATH"))
        for tool in ("latexmk", "synctex", "latexdiff", "pandoc"):
            if shutil.which(tool, path=self.env.get("PATH")) is None:
                logger.warning("%s is not on the PATH", tool)
        self.prune()

    # --- Jobs ---

    def job_dir(self, job_id: str) -> Path | None:
        """The folder of a finished job, or None when it is unknown or gone."""
        if not JOB_ID_RE.match(job_id):
            return None
        folder = self.cfg.jobs_dir / job_id
        return folder if (folder / "job.json").is_file() else None

    def job_meta(self, job_id: str) -> tuple[Path, dict] | None:
        folder = self.job_dir(job_id)
        if folder is None:
            return None
        try:
            return folder, json.loads((folder / "job.json").read_text())
        except (OSError, ValueError):
            return None

    def prune(self) -> None:
        """Keep the newest ``keep_jobs`` finished jobs, and that many of every
        workspace, so one busy paper does not take another's SyncTeX away.
        A workspace job holds only its PDF and SyncTeX file. A folder without
        job.json that is not being made was left by a crash."""
        with self.lock:
            active = set(self.active)
        others: list[tuple[float, Path]] = []
        of_workspace: dict[str, list[tuple[float, Path]]] = {}
        for folder in self.cfg.jobs_dir.iterdir():
            if not folder.is_dir() or not JOB_ID_RE.match(folder.name) or folder.name in active:
                continue
            try:
                stamp = ((folder / "job.json").stat().st_mtime, folder)
            except FileNotFoundError:
                shutil.rmtree(folder, ignore_errors=True)
                continue
            found = self.job_meta(folder.name) if (folder / "out").is_dir() else None
            key = found[1].get("workspace") if found else None
            # The jobs of a workspace that was removed count like any others.
            if key is not None and not (self.workspaces_dir / key).is_dir():
                key = None
            (others if key is None else of_workspace.setdefault(key, [])).append(stamp)
        for group in (others, *of_workspace.values()):
            group.sort(reverse=True)
            for _mtime, folder in group[self.cfg.keep_jobs :]:
                shutil.rmtree(folder, ignore_errors=True)

    def prune_workspaces(self) -> None:
        """Keep the ``keep_workspaces`` workspaces used last. A paper whose
        workspace went is sent whole the next time."""

        def used(folder: Path) -> float:
            try:
                return folder.stat().st_mtime
            except OSError:
                return 0.0

        folders = sorted((f for f in self.workspaces_dir.iterdir() if f.is_dir()), key=used, reverse=True)
        for folder in folders[self.cfg.keep_workspaces :]:
            with self.lock:
                workspace = self.workspaces.get(folder.name)
                if workspace is not None and workspace.lock.locked():
                    continue
                self.workspaces.pop(folder.name, None)
            shutil.rmtree(folder, ignore_errors=True)

    def workspace(self, key: str) -> Workspace:
        with self.lock:
            found = self.workspaces.get(key)
            if found is None:
                found = self.workspaces[key] = Workspace(self.workspaces_dir / key)
            return found

    def _check_manifest(self, files: object) -> dict[str, str]:
        if not isinstance(files, dict):
            raise Refused("files must be an object of paths and their SHA-256")
        if len(files) > self.cfg.max_files:
            raise Refused(f"The upload has more than {self.cfg.max_files} entries")
        for path, sha in files.items():
            if clean_path(path) != path:
                raise Refused(f"{path} is not a usable path. Absolute paths and '..' are refused.")
            if not isinstance(sha, str) or not SHA_RE.match(sha):
                raise Refused(f"The hash of {path} is not a SHA-256")
        folders = {path.rsplit("/", i)[0] for path in files for i in range(1, path.count("/") + 1)}
        for path in files:
            if path in folders:
                raise Refused(f"{path} is a file and a folder at once")
        return files

    def prepare(self, key: str, files: object, clean: bool) -> dict:
        """Get a workspace ready for ``files`` ({path: sha256}) and say which
        of them have to be uploaded. ``clean`` forgets the build files."""
        manifest = self._check_manifest(files)
        workspace = self.workspace(key)
        if not workspace.lock.acquire(timeout=self.cfg.timeout):
            raise Busy("This paper's workspace is busy. Try again in a moment.")
        try:
            workspace.load()
            workspace.sweep(manifest)
            if clean:
                workspace.drop_seeds()
                # From nothing means that the font caches and biber's files go too.
                shutil.rmtree(workspace.home, ignore_errors=True)
            need = [path for path, sha in manifest.items() if not workspace.intact(path, sha)]
            token = secrets.token_hex(16)
            workspace.pending = _Pending(token, manifest, need)
            return {"need": need, "token": token}
        finally:
            workspace.lock.release()

    def cancel(self, key: str) -> bool:
        with self.lock:
            workspace = self.workspaces.get(key)
        return workspace.cancel() if workspace is not None else False

    def forget(self, key: str) -> None:
        """The paper was deleted, so its files and its jobs go now."""
        workspace = self.workspace(key)
        workspace.cancel()
        if not workspace.lock.acquire(timeout=self.cfg.timeout):
            raise Busy("This paper's workspace is busy. Try again in a moment.")
        try:
            with self.lock:
                self.workspaces.pop(key, None)
            shutil.rmtree(workspace.folder, ignore_errors=True)
            for folder in self.cfg.jobs_dir.iterdir():
                if not JOB_ID_RE.match(folder.name) or not (folder / "out").is_dir():
                    continue
                found = self.job_meta(folder.name)
                if found is not None and found[1].get("workspace") == key:
                    shutil.rmtree(folder, ignore_errors=True)
        finally:
            workspace.lock.release()

    def run_in_workspace(self, key: str, token: str, body: bytes, main_file: str, compiler: str) -> dict:
        """Compile in a workspace, after ``prepare``. ``body`` is a tar.gz of
        the files that said it needs. Returns what ``run_job`` returns, with
        status ``cancelled`` and no ``job_id`` when a newer edit stopped it."""
        workspace = self.workspace(key)
        if not workspace.lock.acquire(timeout=self.cfg.timeout):
            raise Busy("This paper's workspace is busy. Try again in a moment.")
        try:
            pending, workspace.pending = workspace.pending, None
            if pending is None or not hmac.compare_digest(pending.token.encode(), token.encode()):
                raise Stale("This compile was not prepared, or another one was prepared since.")
            workspace.begin()
            if not self.slots.acquire(timeout=self.cfg.timeout):
                raise Busy("Every compile worker is busy. Try again in a moment.")
            job_id = secrets.token_hex(16)
            with self.lock:
                self.active.add(job_id)
            job = self.cfg.jobs_dir / job_id
            try:
                job.mkdir(mode=0o700)
                try:
                    workspace.apply(body, pending, self.cfg)
                except Refused:
                    shutil.rmtree(job, ignore_errors=True)
                    raise
                box = self._box(workspace.home, job / "tmp", workspace.tree)
                try:
                    result = self._compile(job, workspace.tree, main_file, compiler, False, box, workspace)
                finally:
                    shutil.rmtree(box.tmp, ignore_errors=True)
                    workspace.trim_home(self.cfg.max_home)
                if result["status"] == "cancelled":
                    shutil.rmtree(job, ignore_errors=True)
                    return {"job_id": None, **result}
                self._finish_workspace_job(job, workspace, main_file, compiler, result)
                return {"job_id": job_id, **result}
            finally:
                with self.lock:
                    self.active.discard(job_id)
                self.slots.release()
                self.prune()
                self.prune_workspaces()
        finally:
            workspace.end()
            workspace.lock.release()

    def _finish_workspace_job(self, job: Path, workspace: Workspace, main_file: str, compiler: str, result: dict) -> None:
        """Move the PDF and the SyncTeX file to the job, where they stay when
        the workspace moves on, and keep the build files of a run that ended."""
        main = workspace.tree / tex_name(main_file)
        (job / "out").mkdir()
        for suffix in (".pdf", ".synctex.gz"):
            with contextlib.suppress(FileNotFoundError):
                os.replace(main.parent / f"{main.stem}{suffix}", job / "out" / f"{main.stem}{suffix}")
        if result["status"] in ("success", "errors"):
            log = main.parent / f"{main.stem}.log"
            skip = {log.relative_to(workspace.tree).as_posix()}
            if is_markdown(main_file):
                skip.add(tex_name(main_file))  # pandoc makes it again every time
            workspace.save_seeds(main_file, compiler, skip)
        meta = {"main_file": main_file, "compiler": compiler, "kind": "pdf", "workspace": workspace.folder.name, **result}
        meta.pop("log")
        (job / "output.log").write_text(result["log"], encoding="utf-8")
        (job / "job.json").write_text(json.dumps(meta))

    def run_job(self, body: bytes, main_file: str, compiler: str, diff: bool) -> dict:
        """Unpack, (latexdiff,) compile. Returns ``{job_id, status,
        duration_ms, log}``. Raises Refused for an upload that is not
        accepted and Busy when no worker became free in time."""
        if not self.slots.acquire(timeout=self.cfg.timeout):
            raise Busy("Every compile worker is busy. Try again in a moment.")
        job_id = secrets.token_hex(16)
        with self.lock:
            self.active.add(job_id)
        job = self.cfg.jobs_dir / job_id
        try:
            job.mkdir(mode=0o700)
            tree = job / "tree"
            tree.mkdir()
            try:
                if diff:
                    (job / "old").mkdir()
                    unpack(body, {"old": job / "old", "new": tree}, self.cfg)
                else:
                    unpack(body, {"": tree}, self.cfg)
            except Refused:
                shutil.rmtree(job, ignore_errors=True)
                raise
            box = self._box(job / "home", job / "tmp", tree, *([job / "old"] if diff else []))
            try:
                result = self._compile(job, tree, main_file, compiler, diff, box)
            finally:
                # A job without a workspace keeps no caches. biber alone
                # unpacks 120 MB of itself.
                shutil.rmtree(box.home, ignore_errors=True)
                shutil.rmtree(box.tmp, ignore_errors=True)
            meta = {"main_file": main_file, "compiler": compiler, "kind": "pdfdiff" if diff else "pdf", **result}
            meta.pop("log")
            (job / "output.log").write_text(result["log"], encoding="utf-8")
            (job / "job.json").write_text(json.dumps(meta))
            return {"job_id": job_id, **result}
        finally:
            with self.lock:
                self.active.discard(job_id)
            self.slots.release()
            self.prune()

    @staticmethod
    def _box(home: Path, tmp: Path, *folders: Path) -> Box:
        """The box of a job whose paper is in ``folders``, with its HOME and
        its TMPDIR, which are made here."""
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        return Box(home, tmp, folders)

    def _run(
        self,
        cmd: list[str],
        cwd: Path,
        box: Box,
        stdout: Path,
        stderr: Path | None,
        deadline: float,
        on_start: Callable[[subprocess.Popen], None] | None = None,
    ) -> tuple[int | None, bool]:
        """Run a program of a job in its box (``Runner.run``). Its HOME and
        TMPDIR are the box's. biber is a packed program that unpacks itself
        below PAR_GLOBAL_TMPDIR and runs from there, so that is the box's
        HOME as well and never a folder that jobs share. FileNotFoundError
        when the program is not installed."""
        if shutil.which(cmd[0], path=self.env.get("PATH")) is None:
            raise FileNotFoundError(f"{cmd[0]} is not installed")
        env = {**self.env, "HOME": str(box.home), "TMPDIR": str(box.tmp), "PAR_GLOBAL_TMPDIR": str(box.home)}
        return self.runner.run(self.sandbox.command(cmd, box), cwd, env, stdout, stderr, deadline, on_start)

    def _compile(
        self, job: Path, tree: Path, main_file: str, compiler: str, diff: bool, box: Box, workspace: Workspace | None = None
    ) -> dict:
        started = time.monotonic()
        deadline = started + self.cfg.timeout

        def done(status: str, log: str) -> dict:
            if len(log) > MAX_LOG_CHARS:
                log = log[-MAX_LOG_CHARS:]
            return {"status": status, "duration_ms": int((time.monotonic() - started) * 1000), "log": log}

        main = tree / main_file
        if not main.is_file() or main.is_symlink():
            return done("failure", f"The main file {main_file} is not in the project.\n")
        markdown = is_markdown(main_file)
        if diff:
            # Markdown is diffed as the LaTeX pandoc makes of each side.
            problem = self._pandoc_both(job, tree, main_file, deadline, box) if markdown else None
            if problem is None:
                problem = self._latexdiff(job, tree, tex_name(main_file), deadline, box)
            if problem is not None:
                return done(*problem)

        tex = tree / tex_name(main_file)
        self._clear_results(tex)
        seeded = workspace is not None and workspace.restore_seeds(main_file, compiler)
        status, log = self._build(job, tree, main_file, compiler, deadline, box, workspace, markdown and not diff)
        if seeded and workspace is not None and _seeds_to_blame(status, log):
            workspace.drop_seeds()
            workspace.sweep(workspace.files or {})
            self._clear_results(tex)
            status, log = self._build(job, tree, main_file, compiler, deadline, box, workspace, markdown and not diff)
        return done(status, log)

    def _build(
        self,
        job: Path,
        tree: Path,
        main_file: str,
        compiler: str,
        deadline: float,
        box: Box,
        workspace: Workspace | None,
        pandoc: bool,
    ) -> tuple[str, str]:
        """latexmk on the main file's LaTeX, which with ``pandoc`` pandoc
        makes of the Markdown first. ``(status, log)``."""
        tex = tree / tex_name(main_file)
        if not pandoc:
            return self._latexmk(job, tex, compiler, deadline, box, workspace)
        problem = self._pandoc(job, tree, main_file, deadline, box, workspace)
        if problem is not None:
            return problem
        status, log = self._latexmk(job, tex, compiler, deadline, box, workspace)
        lines = json.loads((job / MARKDOWN_LINES).read_text())
        return status, map_markdown_log(log, main_file, lines)

    def _pandoc(
        self,
        job: Path,
        folder: Path,
        main_file: str,
        deadline: float,
        box: Box,
        workspace: Workspace | None = None,
        lines: bool = True,
    ) -> tuple[str, str] | None:
        """Turn the Markdown file ``main_file`` of ``folder`` into LaTeX next
        to it. With ``lines`` the LaTeX keeps the Markdown's line numbers, and
        the job keeps them for SyncTeX (MARKDOWN_LINES). None when that
        worked, else ``(status, log)``."""
        if workspace is not None and workspace.stopped():
            return "cancelled", ""
        md = folder / main_file
        tex = folder / tex_name(main_file)
        if tex.is_dir():
            return "failure", _own_message(f"{tex_name(main_file)} is a folder, where the LaTeX of {main_file} goes.")
        tex.unlink(missing_ok=True)  # a file of the paper with that name loses
        cmd = [
            "pandoc",
            "--sandbox",
            # Not there, so pandoc has only its own templates.
            f"--data-dir={job / 'pandoc-data'}",
            f"--from={MARKDOWN_FROM}{'+sourcepos' if lines else ''}",
            "--to=latex",
            "--standalone",
            # The lines of a paragraph stay the lines of the Markdown.
            "--wrap=preserve",
            f"--lua-filter={MARKDOWN_FILTER}",
            f"--output={tex.name}",
            md.name,
        ]
        on_start = workspace.started if workspace is not None else None
        try:
            rc, timed_out = self._run(cmd, md.parent, box, job / "pandoc.out", job / "pandoc.err", deadline, on_start)
        except FileNotFoundError:
            return "failure", _own_message("pandoc is not installed here, so Markdown cannot be compiled.")
        if workspace is not None:
            workspace.ended()
            if workspace.stopped():
                return "cancelled", ""
        errors = _read_text(job / "pandoc.err").strip()
        if timed_out:
            return "timeout", f"pandoc took longer than {self.cfg.timeout} seconds and was stopped.\n{errors}\n"
        if rc != 0 or not tex.is_file():
            first = errors.splitlines()[0] if errors else f"exit code {rc}"
            return "failure", f"pandoc could not read {main_file}.\n{errors}\n" + _own_message(f"pandoc could not read {main_file}: {first}")
        if lines:
            (job / MARKDOWN_LINES).write_text(json.dumps(markdown_lines(_read_text(tex))))
        return None

    def _pandoc_both(self, job: Path, tree: Path, main_file: str, deadline: float, box: Box) -> tuple[str, str] | None:
        """The LaTeX of the older and the newer Markdown, for latexdiff."""
        old = job / "old"
        if not (old / main_file).is_file() or (old / main_file).is_symlink():
            return "failure", f"The main file {main_file} is not in the older version.\n"
        return self._pandoc(job, old, main_file, deadline, box, lines=False) or self._pandoc(
            job, tree, main_file, deadline, box, lines=False
        )

    @staticmethod
    def _clear_results(main: Path) -> None:
        """A PDF or log that came with the upload must not pass for the result."""
        for suffix in (".pdf", ".log", ".synctex.gz", ".fdb_latexmk", ".fls"):
            (main.parent / f"{main.stem}{suffix}").unlink(missing_ok=True)

    def _latexmk(
        self, job: Path, main: Path, compiler: str, deadline: float, box: Box, workspace: Workspace | None
    ) -> tuple[str, str]:
        """Run latexmk on ``main``. ``(status, log)``."""
        if workspace is not None and workspace.stopped():
            return "cancelled", ""
        workdir, stem = main.parent, main.stem
        cmd = [
            "latexmk",
            "-norc",
            *latexmk_engine(compiler, self.texmf),
            *latexmk_biber(),
            "-f",
            "-interaction=nonstopmode",
            "-file-line-error",
            "-synctex=1",
            "-no-shell-escape",
            main.name,
        ]
        on_start = workspace.started if workspace is not None else None
        rc, timed_out = self._run(cmd, workdir, box, job / "latexmk.out", None, deadline, on_start)
        if workspace is not None:
            workspace.ended()
            if workspace.stopped():
                return "cancelled", ""

        tex_log = _read_text(workdir / f"{stem}.log")
        pdf = workdir / f"{stem}.pdf"
        if timed_out:
            status = "timeout"
        elif not pdf.is_file():
            status = "failure"
        elif rc == 0 and not _ERROR_RE.search(tex_log):
            status = "success"
        else:
            status = "errors"
        log = tex_log
        if timed_out:
            log += f"\nThe compile took longer than {self.cfg.timeout} seconds and was stopped.\n"
        if status != "success":
            output = _read_text(job / "latexmk.out")
            tail = _tail_lines(output, LATEXMK_TAIL_LINES)
            # What the guards said (biber_guard) stays, however early it came.
            ours = [line for line in output.splitlines() if line.startswith(GUARD_PREFIX) and line not in tail]
            log += f"\n{LATEXMK_MARK}\n" + "".join(f"{line}\n" for line in ours) + f"{tail}\n"
        return status, log

    def _latexdiff(self, job: Path, tree: Path, main_file: str, deadline: float, box: Box) -> tuple[str, str] | None:
        """Replace the main file in ``tree`` with ``latexdiff --flatten`` of
        the old and the new one. None when that worked, else (status, log)."""
        old_main = job / "old" / main_file
        if not old_main.is_file() or old_main.is_symlink():
            return "failure", f"The main file {main_file} is not in the older version.\n"
        try:
            check_includes(job / "old")
            check_includes(tree)
        except Refused as e:
            return "failure", f"{e}.\n"
        cmd = ["latexdiff", "--flatten", f"old/{main_file}", f"tree/{main_file}"]
        rc, timed_out = self._run(cmd, job, box, job / "diff.tex", job / "latexdiff.err", deadline)
        errors = _read_text(job / "latexdiff.err")
        if timed_out:
            return "timeout", f"latexdiff took longer than {self.cfg.timeout} seconds and was stopped.\n{errors}"
        diff = job / "diff.tex"
        if rc != 0 or not diff.is_file() or diff.stat().st_size == 0:
            return "failure", f"latexdiff failed (exit code {rc}).\n{errors}"
        os.replace(diff, tree / main_file)
        return None

    # --- SyncTeX ---

    def pdf_of(self, job_id: str) -> tuple[Path, Path]:
        """``(tree, pdf)`` of a job. Raises UnknownJob or NoPdf."""
        found = self.job_meta(job_id)
        if found is None:
            raise UnknownJob(job_id)
        folder, meta = found
        main_file = tex_name(meta["main_file"])
        if meta.get("workspace"):
            # TeX ran in the workspace, so its SyncTeX file names paths there.
            tree = self.workspaces_dir / meta["workspace"] / "tree"
            pdf = folder / "out" / f"{Path(main_file).stem}.pdf"
        else:
            tree = folder / "tree"
            main = tree / main_file
            pdf = main.parent / f"{main.stem}.pdf"
        if meta.get("status") not in ("success", "errors") or not pdf.is_file():
            raise NoPdf(job_id)
        return tree, pdf

    def _synctex(self, args: list[str], cwd: Path) -> list[dict[str, str]]:
        out = subprocess.run(
            ["synctex", *args],
            cwd=cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SYNCTEX_SECONDS,
            check=False,
        )
        return _synctex_records(out.stdout)

    def markdown_of(self, job_id: str) -> tuple[str, list[int]] | None:
        """``(main file, the Markdown line of every line of its LaTeX)`` of a
        job that compiled Markdown, or None. A pdfdiff has no lines."""
        found = self.job_meta(job_id)
        if found is None or not is_markdown(found[1].get("main_file", "")):
            return None
        try:
            lines = json.loads((found[0] / MARKDOWN_LINES).read_text())
        except (OSError, ValueError):
            return None
        return found[1]["main_file"], [int(n) for n in lines]

    def synctex_edit(self, job_id: str, page: int, x: float, y: float) -> dict | None:
        """The source position at a point of the PDF (PDF points from the
        page's top left): ``{file, line, column}``, with ``file`` relative to
        the project. None when SyncTeX knows nothing there. A place in the
        LaTeX of a Markdown file is its place in the Markdown."""
        tree, pdf = self.pdf_of(job_id)
        markdown = self.markdown_of(job_id)
        root = os.path.realpath(tree)
        for record in self._synctex(["edit", "-o", f"{page}:{x}:{y}:{pdf}"], pdf.parent):
            name = record.get("Input", "")
            path = os.path.realpath(os.path.join(pdf.parent, name))
            if not name or not path.startswith(root + os.sep):
                continue  # nothing, or a file of TeX Live rather than of the project
            try:
                line, column = int(record.get("Line", "0")), int(record.get("Column", "0"))
            except ValueError:
                continue
            if line >= 1:
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                if markdown is not None and rel == tex_name(markdown[0]):
                    return {"file": markdown[0], "line": markdown_line(markdown[1], line), "column": 0}
                # SyncTeX rarely knows the column and says -1 then.
                return {"file": rel, "line": line, "column": max(column, 0)}
        return None

    def synctex_view(self, job_id: str, file: str, line: int, column: int) -> list[dict]:
        """The boxes of the PDF that show a source position:
        ``[{page, x, y, width, height}]`` in PDF points from the top left.
        A Markdown file's line is looked for in its LaTeX."""
        tree, pdf = self.pdf_of(job_id)
        rel = clean_path(file)
        # A workspace that was removed since no longer says which files it had.
        if rel is None or (tree.is_dir() and not (tree / rel).is_file()):
            raise Refused(f"{file} is not a file of this compile")
        markdown = self.markdown_of(job_id)
        if markdown is not None and rel == markdown[0]:
            # A workspace makes the LaTeX again for every compile, SyncTeX
            # only needs its name.
            rel, line, column = tex_name(rel), latex_line(markdown[1], line), 0
        boxes: list[dict] = []
        for record in self._synctex(["view", "-i", f"{line}:{column}:{tree / rel}", "-o", str(pdf)], pdf.parent):
            try:
                page = int(record["Page"])
                h, v = float(record["h"]), float(record["v"])
                width, height = float(record["W"]), float(record["H"])
            except (KeyError, ValueError):
                continue
            # SyncTeX's v is the bottom of the box (baseline plus depth), and
            # H is its height plus depth.
            box = {
                "page": page,
                "x": round(h, 2),
                "y": round(v - height, 2),
                "width": round(width, 2),
                "height": round(height, 2),
            }
            if box not in boxes:
                boxes.append(box)
        return boxes


def _synctex_records(out: str) -> list[dict[str, str]]:
    """The records between "SyncTeX result begin" and "end". Each one starts
    with an ``Output:`` line."""
    records: list[dict[str, str]] = []
    inside = False
    for line in out.splitlines():
        if line == "SyncTeX result begin":
            inside = True
        elif line == "SyncTeX result end":
            inside = False
        elif inside and ":" in line:
            key, _, value = line.partition(":")
            if key == "Output" or not records:
                records.append({})
            records[-1][key] = value
    return records


# --- HTTP -------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: Service) -> None:
        self.service = service
        super().__init__((service.cfg.host, service.cfg.port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "meertex-compile"
    # Seconds a single read or write on the connection may take.
    timeout = 120

    # --- Responses ---

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, detail: str, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps({"detail": detail}).encode(), "application/json", extra)

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.service.cfg.token}"
        if hmac.compare_digest(given.encode(), expected.encode()):
            return True
        self._error(401, "A valid Authorization: Bearer <COMPILE_TOKEN> is required", {"WWW-Authenticate": "Bearer"})
        return False

    def _body(self, limit: int) -> bytes | None:
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            self._error(411, "A Content-Length is required")
            return None
        size = int(length)
        if size > limit:
            self.close_connection = True
            self._error(413, f"The body is larger than {limit} bytes")
            return None
        data = self.rfile.read(size)
        if len(data) != size:
            self._error(400, "The body ended early")
            return None
        return data

    def _json_body(self, limit: int = MAX_JSON_BODY) -> dict | None:
        data = self._body(limit)
        if data is None:
            return None
        try:
            parsed = json.loads(data or b"{}")
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            self._error(400, "The body must be a JSON object")
            return None
        return parsed

    # --- Routes ---

    def do_GET(self) -> None:
        self._guarded(self._get)

    def do_POST(self) -> None:
        self._guarded(self._post)

    def _guarded(self, handle: Callable[[], None]) -> None:
        """Answer 500 with the reason instead of dropping the connection."""
        try:
            handle()
        except (BrokenPipeError, ConnectionResetError):
            return  # the client went away
        except Exception as e:
            logger.exception("%s %s failed", self.command, self.path)
            with contextlib.suppress(OSError):
                self._error(500, f"The compile service failed ({e.__class__.__name__}: {e})")

    def _get(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        if not self._authorized():
            return
        match = JOB_PATH_RE.match(path)
        if match is None or match[2] not in ("output.pdf", "output.log"):
            self._error(404, "Not found")
            return
        found = self.server.service.job_meta(match[1])
        if found is None:
            self._error(404, "No such job. Old jobs are removed.")
            return
        folder, _meta = found
        if match[2] == "output.log":
            self._send(200, (folder / "output.log").read_bytes(), "text/plain; charset=utf-8")
            return
        try:
            _tree, pdf = self.server.service.pdf_of(match[1])
        except (UnknownJob, NoPdf):
            self._error(404, "This job made no PDF")
            return
        with pdf.open("rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.end_headers()
            shutil.copyfileobj(f, self.wfile)

    def _post(self) -> None:
        url = urlsplit(self.path)
        if not self._authorized():
            return
        service = self.server.service
        workspace_route = WORKSPACE_PATH_RE.match(url.path)
        if workspace_route is not None:
            self._workspace_route(workspace_route[1], workspace_route[2])
            return
        if url.path in ("/jobs", "/latexdiff"):
            query = parse_qs(url.query)
            main_file = clean_path(query.get("main_file", ["main.tex"])[0])
            compiler = query.get("compiler", ["pdflatex"])[0]
            workspace = query.get("workspace", [""])[0]
            token = query.get("token", [""])[0]
            if workspace and (url.path != "/jobs" or not WORKSPACE_RE.match(workspace)):
                self._error(400, "workspace is the id of a paper, and only for /jobs")
                return
            if main_file is None:
                self._error(400, "main_file must be a relative path inside the project")
                return
            if not all(MAIN_FILE_PART_RE.match(part) for part in main_file.split("/")):
                self._error(
                    400,
                    f"The main file {main_file} cannot be compiled. Its name may hold letters, digits, spaces "
                    "and . , + = @ - and must start with a letter or a digit.",
                )
                return
            if compiler not in COMPILERS:
                self._error(400, f"compiler is one of {', '.join(COMPILERS)}")
                return
            body = self._body(service.cfg.max_upload)
            if body is None:
                return
            try:
                if workspace:
                    result = service.run_in_workspace(workspace, token, body, main_file, compiler)
                else:
                    result = service.run_job(body, main_file, compiler, diff=url.path == "/latexdiff")
            except Refused as e:
                self._error(400, str(e))
                return
            except Stale as e:
                self._error(409, str(e))
                return
            except Busy as e:
                self._error(503, str(e))
                return
            self._json(200, result)
            return
        match = JOB_PATH_RE.match(url.path)
        if match is None or not match[2].startswith("synctex/"):
            self._error(404, "Not found")
            return
        body = self._json_body()
        if body is None:
            return
        try:
            if match[2] == "synctex/edit":
                page, x, y = int(body["page"]), float(body["x"]), float(body["y"])
                if page < 1 or not (math.isfinite(x) and math.isfinite(y)):
                    raise ValueError("page, x or y")
                found = service.synctex_edit(match[1], page, x, y)
                if found is None:
                    self._error(422, "SyncTeX knows no source line at that point")
                    return
                self._json(200, found)
            else:
                file, line, column = str(body["file"]), int(body["line"]), int(body.get("column") or 0)
                if line < 1 or column < 0:
                    raise ValueError("line or column")
                self._json(200, service.synctex_view(match[1], file, line, column))
        except (KeyError, TypeError, ValueError):
            self._error(400, "Give page, x and y (edit) or file, line and column (view), as numbers")
        except UnknownJob:
            self._error(404, "No such job. Old jobs are removed.")
        except NoPdf:
            self._error(404, "This job made no PDF")
        except Refused as e:
            self._error(422, str(e))
        except subprocess.TimeoutExpired:
            self._error(504, "SyncTeX took too long")

    def _workspace_route(self, key: str, action: str) -> None:
        service = self.server.service
        if action == "cancel":
            self._json(200, {"cancelled": service.cancel(key)})
            return
        if action == "forget":
            try:
                service.forget(key)
            except Busy as e:
                self._error(503, str(e))
                return
            self._json(200, {"forgotten": True})
            return
        body = self._json_body(MAX_MANIFEST_BODY)
        if body is None:
            return
        try:
            self._json(200, service.prepare(key, body.get("files"), bool(body.get("clean"))))
        except Refused as e:
            self._error(400, str(e))
        except Busy as e:
            self._error(503, str(e))

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {fmt % args}\n")


def main() -> None:
    if sys.argv[1:2] == ["--biber-guard"]:
        sys.exit(biber_guard(sys.argv[2:]))
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    cfg = Config.from_env()
    service = Service(cfg)
    server = Server(service)

    def stop(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    print(
        f"meertex-compile on http://{cfg.host}:{cfg.port}, TeX Live in {service.texmf}, "
        f"{service.sandbox} in {cfg.jobs_dir}",
        file=sys.stderr,
        flush=True,
    )
    if not service.sandbox.on:
        logger.warning(
            "Without the sandbox a lualatex document reads and writes every file this user can, "
            "the other papers' and the container's environment in /proc among them (compile/README.md)."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.runner.kill_all()
        server.server_close()


if __name__ == "__main__":
    main()
