"""The live document and the git history (docs/DESIGN.md §8, §9).

This module is the interface the rest of the server uses. Everything here is
synchronous and safe to call from FastAPI's threadpool (plain ``def`` routes).
Every document access goes through the project's room (``rooms.py``), so the
API, the sync connections and the snapshots all see one document.

Refs (§7): ``live`` is the current document, everything else goes to git and
is checked with ``git.safe_ref`` first. A ref or path that cannot be used
raises ``Refused`` (a ValueError), one that names nothing raises ``NotFound``
(a KeyError), so routes can answer 422 and 404.
"""

import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import get_settings
from . import blobs, git, rooms, ydoc

logger = logging.getLogger("uvicorn.error")

EDITS_BY = "Edits by "


class NotFound(KeyError):
    """A ref or a path that names nothing. ``args[0]`` says which."""


class Refused(ValueError):
    """A ref or a path that cannot be used. ``args[0]`` says why."""


class Exists(Refused):
    """A create-only write found the file there already."""

BASIC_MAIN = r"""\documentclass{article}

\title{Untitled}
\author{}

\begin{document}
\maketitle

\section{Introduction}

\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""


def _file_bytes(project_id: str, files: dict[str, ydoc.Entry]) -> dict[str, bytes]:
    """Contents by path, as the working tree gets them. A blob whose bytes never
    arrived is left out rather than written empty."""
    out: dict[str, bytes] = {}
    for path, entry in files.items():
        if entry.kind == "text":
            out[path] = (entry.text or "").encode()
        else:
            data = blobs.read(project_id, entry.sha or "")
            if data is not None:
                out[path] = data
    # A file and a folder of the same name cannot both be on disk. The API
    # refuses to make one, but two offline edits can. The folder wins, because
    # it may hold many files, and the file waits in the live document.
    folders = {path.rsplit("/", i)[0] for path in out for i in range(1, path.count("/") + 1)}
    for path in folders & out.keys():
        logger.warning("Project %s: %s is a file and a folder, the file is left out of the snapshot", project_id, path)
        del out[path]
    return out


def _message(editors: set[str]) -> str:
    if not editors:
        return "Snapshot"
    return EDITS_BY + ", ".join(sorted(editors))


def _editors(message: str) -> list[str]:
    first = message.split("\n", 1)[0]
    if not first.startswith(EDITS_BY):
        return []
    return [e.strip() for e in first[len(EDITS_BY) :].split(",") if e.strip()]


def init_project(project_id: str, template: str = "empty") -> str:
    """Create the project's data directory, its git repository with a first
    commit, and its (empty, or ``basic``: main.tex + refs.bib) live document.
    Returns the newest commit."""
    git.init(project_id, "Project created")
    if template == "basic":
        return add_starter_files(project_id)
    return git.head(project_id)


def add_starter_files(project_id: str, editor: str | None = None) -> str:
    """Write the ``basic`` starter (main.tex and refs.bib) into the live
    document and commit it as "Starter files". Returns the newest commit."""
    room = rooms.get(project_id)
    with room.edit(editor) as doc, doc.transaction():
        ydoc.add_text(doc, "main.tex", BASIC_MAIN)
        ydoc.add_text(doc, "refs.bib", "")
    room.flush()
    with git.lock(project_id):
        files, _editors = room.take_for_snapshot()
        git.write_tree(project_id, _file_bytes(project_id, files))
        git.commit_all(project_id, "Starter files")
    return git.head(project_id)


def fork_project(src_id: str, dst_id: str, message: str) -> str:
    """Start project ``dst_id`` from ``src_id``'s live files: a new document and
    a new repository whose one commit carries ``message``. Returns that commit."""
    with rooms.get(src_id).read() as doc:
        files = ydoc.materialize(doc)
    blobs.copy_all(src_id, dst_id, {e.sha for e in files.values() if e.kind == "blob" and e.sha})
    git.init(dst_id, "Project created")
    room = rooms.get(dst_id)
    with room.edit() as doc, doc.transaction():
        for path, entry in files.items():
            if entry.kind == "text":
                ydoc.add_text(doc, path, entry.text or "")
            else:
                ydoc.add_blob(doc, path, entry.sha or "", entry.size or 0)
    files, _editors = room.take_for_snapshot()
    with git.lock(dst_id):
        git.write_tree(dst_id, _file_bytes(dst_id, files))
        commit = git.commit_all(dst_id, message)
    return commit or git.head(dst_id)


def delete_project(project_id: str) -> None:
    """Close the project's room and its sync connections (4004), and remove
    DATA_DIR/projects/<id>. The database rows go with the project row (ON
    DELETE CASCADE)."""
    rooms.drop(project_id)
    # Not in the middle of a snapshot. One that waits for the lock finds no
    # repository and gives up (git.write_tree).
    with git.lock(project_id):
        git.remove(project_id)


def member_left(project_id: str, email: str) -> None:
    """Close the sync connections of someone who is no longer a member."""
    rooms.kick(project_id, email, rooms.CLOSE_NO_PROJECT, "You are no longer a member of this project")


def member_role_changed(project_id: str, email: str) -> None:
    """Close a member's sync connections so they connect again with the new role."""
    rooms.kick(project_id, email, rooms.CLOSE_ROLE_CHANGED, "Your role on this project changed")


def snapshot(project_id: str, editors: list[str] | None = None, *, at: datetime | None = None) -> str:
    """Commit the live document now if it changed since the last commit, and
    return the commit HEAD points to afterwards. ``at`` (naive UTC) dates the
    commit, for the snapshot taken at a deadline."""
    room = rooms.get(project_id)
    room.flush()
    if room.deleted:
        raise NotFound(f"Project {project_id} was deleted")
    with git.lock(project_id):
        files, seen = room.take_for_snapshot()
        try:
            git.write_tree(project_id, _file_bytes(project_id, files))
            git.commit_all(project_id, _message(seen | set(editors or [])), at=at)
        except BaseException:
            room.snapshot_failed(seen)
            raise
        return git.head(project_id)


def commit_at(project_id: str, when: datetime) -> str | None:
    """The last commit at or before ``when`` (naive UTC), or None."""
    return git.commit_at(project_id, when)


def resolve_commit(project_id: str, ref: str) -> str | None:
    """The commit a ref names: ``HEAD``, a tag or a (possibly short) commit.
    None when it names nothing. ``live`` is not a commit, callers handle it."""
    return git.resolve(project_id, ref)


def check_ref(ref: str) -> None:
    """Raise Refused for a ref that must not reach git. ``live`` passes."""
    if ref != "live" and not git.safe_ref(ref):
        raise Refused(f"{ref!r} is not a valid ref")


def commit_of(project_id: str, ref: str) -> str:
    """The commit ``ref`` names. For ``live`` that is a snapshot taken now."""
    check_ref(ref)
    if ref == "live":
        return snapshot(project_id)
    commit = git.resolve(project_id, ref)
    if commit is None:
        raise NotFound(f"Nothing is called {ref}")
    return commit


def create_tag(project_id: str, tag: str, commit: str) -> None:
    with git.lock(project_id):
        git.tag(project_id, tag, commit)


def delete_tag(project_id: str, tag: str) -> None:
    with git.lock(project_id):
        git.untag(project_id, tag)


def store_blob(project_id: str, data: bytes) -> str:
    """Store bytes as a project blob and return their SHA-256 hex."""
    return blobs.store(project_id, data)


def blob_path(project_id: str, sha: str) -> Path | None:
    """Where a stored blob is on disk, or None."""
    return blobs.path(project_id, sha)


# --- Files (§11) ------------------------------------------------------------------


def _info(path: str, e: ydoc.Entry) -> dict:
    size = len((e.text or "").encode()) if e.kind == "text" else e.size or 0
    return {"id": e.id, "path": path, "kind": e.kind, "size": size, "sha": e.content_sha()}


def list_files(project_id: str, ref: str = "live") -> list[dict]:
    """``[{id, path, kind, size, sha}]`` sorted by path, for ``live`` or a ref.
    ``id`` is None for refs other than ``live``."""
    check_ref(ref)
    if ref == "live":
        with rooms.get(project_id).read() as doc:
            files = ydoc.materialize(doc)
        return sorted((_info(path, e) for path, e in files.items()), key=lambda f: f["path"])
    commit = git.resolve(project_id, ref)
    if commit is None:
        raise NotFound(f"Nothing is called {ref}")
    # A commit stores no "kind". Text is what the document would call text,
    # judged the same way the sync client judges a file on disk.
    tree = git.ls_tree(project_id, commit)
    contents = git.read_blobs(project_id, [f["sha"] for f in tree])
    limit = get_settings().max_text_bytes
    out = []
    for f in tree:
        kind = "text" if ydoc.is_text(contents.get(f["sha"], b"\0"), limit) else "blob"
        out.append({"id": None, "path": f["path"], "kind": kind, "size": f["size"], "sha": f["sha"]})
    return out


@dataclass
class FileContent:
    """A file's content: ``data`` in memory, or ``file``, a stored blob on disk."""

    kind: str  # "text" or "blob"
    data: bytes = b""
    file: Path | None = None


def read_file(project_id: str, path: str, ref: str = "live") -> FileContent | None:
    """The content of ``path`` at ``ref``. None when there is no such file, or
    when a blob's bytes were never uploaded."""
    check_ref(ref)
    if ydoc.path_problem(path):
        return None
    if ref == "live":
        with rooms.get(project_id).read() as doc:
            entry = ydoc.find(doc, path)
        if entry is None:
            return None
        if entry.kind == "text":
            return FileContent("text", data=(entry.text or "").encode())
        stored = blobs.path(project_id, entry.sha or "")
        return FileContent("blob", file=stored) if stored else None
    commit = git.resolve(project_id, ref)
    if commit is None:
        raise NotFound(f"Nothing is called {ref}")
    data = git.show(project_id, commit, path)
    if data is None:
        return None
    kind = "text" if ydoc.is_text(data, get_settings().max_text_bytes) else "blob"
    return FileContent(kind, data=data)


def _clash(files: dict[str, ydoc.Entry], path: str, leaving: frozenset[str] = frozenset()) -> str | None:
    """Why ``path`` cannot be a file among ``files``: it would be a file and a
    folder at once. ``leaving`` are paths that move away in the same edit."""
    prefix = path + "/"
    if any(p.startswith(prefix) for p in files if p not in leaving):
        return f"{path} is a folder"
    parts = path.split("/")
    for i in range(1, len(parts)):
        folder = "/".join(parts[:i])
        if folder in files and folder not in leaving:
            return f"{folder} is a file, so it cannot hold {path}"
    return None


def write_file(project_id: str, path: str, data: bytes, editor: str, create_only: bool = False) -> dict:
    """Write ``data`` to ``path`` in the live document and return the file.
    Text is applied as a character diff against what is there, so edits that
    reach the server meanwhile survive. Anything else is stored as a blob.
    Raises Refused for a path that cannot be used. With ``create_only`` it
    raises Exists instead of writing over a file, checked under the room's
    lock, so of two people creating one file at once only the first wins."""
    problem = ydoc.path_problem(path)
    if problem:
        raise Refused(problem)
    settings = get_settings()
    text = data.decode() if ydoc.is_text(data, settings.max_text_bytes) else None
    sha = ""
    if text is None:
        if len(data) > settings.max_blob_bytes:
            raise Refused(f"The file is larger than {settings.max_blob_bytes} bytes")
        # The bytes go first: an entry must never point at a missing blob.
        sha = blobs.store(project_id, data)
    with rooms.get(project_id).edit(editor) as doc:
        files = ydoc.materialize(doc)
        if create_only and path in files:
            raise Exists(f"{path} exists already")
        problem = _clash(files, path)
        if problem:
            raise Refused(problem)
        if text is not None:
            file_id = ydoc.put_text(doc, path, text)
        else:
            file_id = ydoc.put_blob(doc, path, sha, len(data))
        entry = ydoc.get_entry(doc, file_id)
    assert entry is not None
    return _info(path, entry)


def _under(files: dict[str, ydoc.Entry], path: str) -> list[str]:
    """``path`` itself when it is a file, else every file in the folder ``path``."""
    if path in files:
        return [path]
    prefix = path + "/"
    return sorted(p for p in files if p.startswith(prefix))


def delete_file(project_id: str, path: str, editor: str) -> list[str]:
    """Remove a file, or a folder with everything in it. Returns the removed
    paths, empty when ``path`` names nothing."""
    if ydoc.path_problem(path):
        return []
    with rooms.get(project_id).edit(editor) as doc:
        files = ydoc.materialize(doc)
        gone = _under(files, path)
        with doc.transaction():
            for p in gone:
                ydoc.remove(doc, files[p].id)
    return gone


def move_file(project_id: str, src: str, dst: str, editor: str) -> list[dict]:
    """Rename a file, or a folder with everything in it, and return the moved
    files. Ids stay, so edits made meanwhile are kept. Raises NotFound when
    ``src`` names nothing and Refused when ``dst`` cannot be used."""
    if ydoc.path_problem(src):
        raise NotFound(f"There is no file or folder {src}")
    problem = ydoc.path_problem(dst)
    if problem:
        raise Refused(problem)
    with rooms.get(project_id).edit(editor) as doc:
        files = ydoc.materialize(doc)
        moves = {p: dst + p[len(src) :] for p in _under(files, src)}
        if not moves:
            raise NotFound(f"There is no file or folder {src}")
        leaving = frozenset(moves)
        for new in moves.values():
            problem = ydoc.path_problem(new)
            if problem is None and new in files and new not in leaving:
                problem = f"{new} exists already"
            problem = problem or _clash(files, new, leaving)
            if problem:
                raise Refused(problem)
        with doc.transaction():
            for old, new in moves.items():
                ydoc.rename(doc, files[old].id, new)
    return [_info(new, files[old]) for old, new in moves.items()]


def write_zip(project_id: str, ref: str, target: Path) -> None:
    """Write the files at ``ref`` to ``target`` as a zip file."""
    check_ref(ref)
    if ref != "live":
        commit = git.resolve(project_id, ref)
        if commit is None:
            raise NotFound(f"Nothing is called {ref}")
        git.archive(project_id, commit, target)
        return
    with rooms.get(project_id).read() as doc:
        files = ydoc.materialize(doc)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            entry = files[path]
            if entry.kind == "text":
                zf.writestr(path, (entry.text or "").encode())
                continue
            stored = blobs.path(project_id, entry.sha or "")
            if stored is not None:
                # Images and PDFs are compressed already.
                zf.write(stored, path, compress_type=zipfile.ZIP_STORED)


def diff(project_id: str, from_ref: str, to_ref: str, word: bool = False) -> dict:
    """``{from, to, files: [{path, old_path, status, binary, diff}]}`` between two
    refs, with ``from`` and ``to`` as commits. ``live`` is snapshotted first."""
    check_ref(from_ref)
    check_ref(to_ref)
    commits: dict[str, str] = {}
    # live last, so a ref that names nothing fails before a snapshot is made.
    for ref in sorted({from_ref, to_ref}, key=lambda r: r == "live"):
        commits[ref] = commit_of(project_id, ref)
    a, b = commits[from_ref], commits[to_ref]
    return {"from": a, "to": b, "files": git.diff(project_id, a, b, word)}


def history(project_id: str, limit: int = 50) -> list[dict]:
    """``[{commit, parent, at, message, editors, files_changed}]``, newest first. ``at`` is
    naive UTC, ``editors`` come from the message's "Edits by ..." line."""
    commits = git.log(project_id, limit)
    for c in commits:
        c["editors"] = _editors(c["message"])
    return commits
