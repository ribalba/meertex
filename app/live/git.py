"""A project's history as a git repository (docs/DESIGN.md §9).

DATA_DIR/projects/<id>/repo is an ordinary repository with a working tree that
only meertex writes. A snapshot mirrors the live document into the working tree
and commits. Versions are tags.

git always runs as a subprocess with an argument list, never through a shell,
and with the user's and the system's git configuration switched off, so hooks,
signing or aliases on a developer machine cannot change what happens. Refs that
come from a request are checked before they reach git (``safe_ref``), because a
ref that starts with ``-`` would be read as an option.

git never looks above DATA_DIR/projects/<id> for a repository. Without that, a
project whose repository was just deleted would let a late snapshot find the
next repository up the tree, which on a developer machine is meertex's own.
"""

import os
import re
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..config import get_settings

AUTHOR = ("meertex", "meertex@localhost")

# One lock per project: a snapshot (write tree, add, commit) must not interleave
# with another one, and tagging must see a finished commit.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^-]{0,199}$")


class GitError(RuntimeError):
    pass


def lock(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def repo_dir(project_id: str) -> Path:
    return get_settings().projects_dir / project_id / "repo"


def _env(project_id: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if project_id is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(repo_dir(project_id).parent.resolve())
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME=AUTHOR[0],
        GIT_AUTHOR_EMAIL=AUTHOR[1],
        GIT_COMMITTER_NAME=AUTHOR[0],
        GIT_COMMITTER_EMAIL=AUTHOR[1],
    )
    return env


def run(
    project_id: str,
    *args: str,
    check: bool = True,
    input: bytes | None = None,
    env: dict[str, str] | None = None,
) -> bytes:
    """``git <args>`` in the project's repository, with ``env`` added to the
    environment. Returns stdout."""
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "core.quotepath=false", *args],
        cwd=repo_dir(project_id),
        env={**_env(project_id), **(env or {})},
        input=input,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def safe_ref(ref: str) -> bool:
    """Whether ``ref`` may be handed to git as a revision."""
    return bool(_REF_RE.match(ref)) and ".." not in ref and "@{" not in ref


def init(project_id: str, message: str) -> str:
    """Create the repository with one empty commit. Returns its id."""
    repo = repo_dir(project_id)
    repo.mkdir(parents=True, exist_ok=True)
    run(project_id, "init", "-q", "-b", "main")
    run(project_id, "commit", "-q", "--allow-empty", "-m", message)
    return head(project_id)


def remove(project_id: str) -> None:
    shutil.rmtree(get_settings().projects_dir / project_id, ignore_errors=True)
    with _locks_guard:
        _locks.pop(project_id, None)


def head(project_id: str) -> str:
    return run(project_id, "rev-parse", "HEAD").decode().strip()


def write_tree(project_id: str, files: dict[str, bytes]) -> None:
    """Make the working tree hold exactly ``files`` (path -> content). Paths come
    from ydoc.materialize(), which already refused '..' and friends. The check
    below is the second line, in case a caller ever passes something else."""
    repo = repo_dir(project_id).resolve()
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        # The project was deleted while a snapshot was on its way.
        raise GitError(f"no repository for project {project_id}")
    wanted: dict[Path, bytes] = {}
    for rel, data in files.items():
        target = (repo / rel).resolve()
        if repo not in target.parents or target == git_dir or git_dir in target.parents:
            raise GitError(f"refusing to write outside the repository: {rel}")
        wanted[target] = data

    # Remove what is no longer there, then empty directories, deepest first.
    folders: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo, topdown=True):
        if Path(dirpath) == repo:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        else:
            folders.append(Path(dirpath))
        for name in filenames:
            p = Path(dirpath) / name
            if p not in wanted:
                p.unlink()
    for folder in sorted(folders, key=lambda p: -len(p.parts)):
        if not any(folder.iterdir()):
            folder.rmdir()

    # A path that switched between being a folder and a file was already
    # cleared above: the old files were not wanted, and their emptied folders
    # went with them.
    for target, data in wanted.items():
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def commit_all(project_id: str, message: str, *, at: datetime | None = None) -> str | None:
    """Commit the working tree if it differs from HEAD. Returns the new commit,
    or None when there was nothing to commit. ``at`` (naive UTC) dates the
    commit, which is how a deadline snapshot is found by commit_at() later."""
    run(project_id, "add", "-A")
    if not run(project_id, "status", "--porcelain").strip():
        return None
    env = None
    if at is not None:
        stamp = f"@{int(at.replace(tzinfo=UTC).timestamp())} +0000"
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    run(project_id, "commit", "-q", "-m", message, env=env)
    return head(project_id)


def resolve(project_id: str, ref: str) -> str | None:
    if not safe_ref(ref):
        return None
    out = run(project_id, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    sha = out.decode().strip()
    return sha or None


def commit_at(project_id: str, when: datetime) -> str | None:
    """The last commit at or before ``when`` (naive UTC)."""
    stamp = when.strftime("%Y-%m-%d %H:%M:%S +0000")
    out = run(project_id, "rev-list", "-1", f"--before={stamp}", "HEAD", check=False)
    sha = out.decode().strip()
    return sha or None


def tag(project_id: str, name: str, commit: str) -> None:
    if not safe_ref(name) or not safe_ref(commit):
        raise GitError(f"not a usable tag or commit: {name} {commit}")
    run(project_id, "check-ref-format", f"refs/tags/{name}")
    run(project_id, "tag", name, commit)


def untag(project_id: str, name: str) -> None:
    if safe_ref(name):
        run(project_id, "tag", "-d", name, check=False)


def ls_tree(project_id: str, commit: str) -> list[dict]:
    """``[{path, size, sha}]`` of a commit, sorted by path. ``sha`` is git's blob id."""
    out = run(project_id, "ls-tree", "-r", "-l", "-z", commit)
    files = []
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        _mode, kind, sha, size = meta.split()
        if kind != b"blob":
            continue
        files.append({"path": path.decode(), "size": int(size), "sha": sha.decode()})
    return sorted(files, key=lambda f: f["path"])


def read_blobs(project_id: str, shas: list[str]) -> dict[str, bytes]:
    """Many git blobs at once (``git cat-file --batch``), by blob id."""
    if not shas:
        return {}
    out = run(project_id, "cat-file", "--batch", input="".join(f"{s}\n" for s in shas).encode())
    found: dict[str, bytes] = {}
    pos = 0
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].split()
        pos = header_end + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        found[header[0].decode()] = out[pos : pos + size]
        pos += size + 1  # the content is followed by a newline
    return found


def show(project_id: str, commit: str, path: str) -> bytes | None:
    """A file's content at a commit, or None."""
    out = subprocess.run(
        ["git", "cat-file", "blob", f"{commit}:{path}"],
        cwd=repo_dir(project_id),
        env=_env(project_id),
        capture_output=True,
        timeout=60,
        check=False,
    )
    return out.stdout if out.returncode == 0 else None


# --- Reading the history ---------------------------------------------------------

_STATUS = {"A": "added", "D": "deleted", "M": "modified", "T": "modified", "R": "renamed", "C": "added"}
_HEX_RE = re.compile(rb"^[0-9a-f]{40,64}$")
_SHORTSTAT_RE = re.compile(rb"^ (\d+) files? changed")


def _check(*refs: str) -> None:
    for ref in refs:
        if not safe_ref(ref):
            raise GitError(f"not a usable ref: {ref!r}")


def _changes(project_id: str, a: str, b: str) -> list[tuple[str, str | None, str]]:
    """``[(status, old path or None, path)]`` between two commits, renames found."""
    out = run(project_id, "diff", "--no-color", "--no-ext-diff", "-M", "--name-status", "-z", a, b, "--")
    fields = out.split(b"\0")
    changes = []
    i = 0
    while i < len(fields) and fields[i]:
        letter = chr(fields[i][0])
        if letter in "RC":
            changes.append((_STATUS[letter], fields[i + 1].decode(), fields[i + 2].decode()))
            i += 3
        else:
            changes.append((_STATUS.get(letter, "modified"), None, fields[i + 1].decode()))
            i += 2
    return changes


def _split_patch(patch: bytes) -> list[bytes]:
    """One chunk per file. Content lines in a patch always start with a prefix
    (space, +, -, ~), so a line starting with "diff --git " is a file header."""
    chunks: list[bytes] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith(b"diff --git ") or not chunks:
            chunks.append(line)
        else:
            chunks[-1] += line
    return chunks


def _is_binary(chunk: bytes) -> bool:
    for line in chunk.splitlines():
        if line.startswith(b"@@"):
            return False
        if line.startswith(b"Binary files ") or line == b"GIT binary patch":
            return True
    return False


def diff(project_id: str, a: str, b: str, word: bool = False) -> list[dict]:
    """``[{path, old_path, status, binary, diff}]`` from commit ``a`` to ``b``.
    ``diff`` is a unified diff with 3 lines of context, or git's
    ``--word-diff=porcelain`` output, and empty for binary files."""
    _check(a, b)
    changes = _changes(project_id, a, b)
    if not changes:
        return []
    args = ["diff", "--no-color", "--no-ext-diff", "-M", "-U3"]
    if word:
        args.append("--word-diff=porcelain")
    chunks = _split_patch(run(project_id, *args, a, b, "--"))
    if len(chunks) != len(changes):
        raise GitError(f"git diff gave {len(chunks)} patches for {len(changes)} files")
    files = []
    for (status, old_path, path), chunk in zip(changes, chunks, strict=True):
        binary = _is_binary(chunk)
        files.append({
            "path": path,
            "old_path": old_path,
            "status": status,
            "binary": binary,
            "diff": "" if binary else chunk.decode(errors="replace"),
        })
    return files


def log(project_id: str, limit: int) -> list[dict]:
    """The newest ``limit`` commits on HEAD: ``[{commit, parent, at, message,
    files_changed}]``. ``at`` is a naive UTC datetime, ``parent`` the first
    parent or None for the first commit."""
    # -z ends every commit with NUL, and a commit message cannot hold one, so
    # the fields come in fours.
    out = run(project_id, "log", "-z", f"--max-count={limit}", "--format=%H%x00%P%x00%ct%x00%B", "HEAD")
    fields = out.split(b"\0")
    commits = []
    for i in range(0, len(fields) - 3, 4):
        sha, parents, stamp, message = fields[i : i + 4]
        commits.append({
            "commit": sha.decode(),
            "parent": (parents.split() or [b""])[0].decode() or None,
            "at": datetime.fromtimestamp(int(stamp), UTC).replace(tzinfo=None),
            "message": message.decode(errors="replace").strip(),
            "files_changed": 0,
        })
    # A second pass for the number of files, which --shortstat prints after
    # each commit id (and leaves out for an empty commit).
    counts: dict[str, int] = {}
    current = None
    for line in run(project_id, "log", f"--max-count={limit}", "--format=%H", "--shortstat", "HEAD").splitlines():
        if _HEX_RE.match(line):
            current = line.decode()
        elif current and (m := _SHORTSTAT_RE.match(line)):
            counts[current] = int(m.group(1))
    for c in commits:
        c["files_changed"] = counts.get(c["commit"], 0)
    return commits


def archive(project_id: str, commit: str, target: Path) -> None:
    """Write the files of ``commit`` to ``target`` as a zip file."""
    _check(commit)
    run(project_id, "archive", "--format=zip", f"--output={target}", commit)


# --- Serving clones ------------------------------------------------------------

# What a Git-Protocol header may hold ("version=2", several joined by ":").
_PROTOCOL_RE = re.compile(r"^[A-Za-z0-9=:._-]{1,200}$")


def upload_pack(project_id: str, protocol: str | None, *, advertise: bool = False) -> tuple[list[str], dict[str, str]]:
    """``(argv, env)`` of the ``git upload-pack`` that serves a clone of the
    project over HTTP, in git's stateless RPC mode. ``protocol`` is the
    client's Git-Protocol header, ``advertise`` lists the refs instead of
    sending objects. Only upload-pack ever serves a repository, so nothing
    from outside writes to it."""
    argv = ["git", "-c", "core.hooksPath=/dev/null", "upload-pack", "--stateless-rpc"]
    if advertise:
        argv.append("--advertise-refs")
    env = _env(project_id)
    if protocol and _PROTOCOL_RE.match(protocol):
        env["GIT_PROTOCOL"] = protocol
    return [*argv, str(repo_dir(project_id))], env
