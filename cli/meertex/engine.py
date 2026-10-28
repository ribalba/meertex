"""The sync client's reconcile engine (docs/DESIGN.md §10).

A checkout folder and the project's live document are kept equal file by
file. Each path has a *shadow*, what the disk and the document last agreed
on. It is the common base of a three-way comparison with the disk as our side
and the document as everybody else's. A local change goes into the document
as a character diff from the shadow (merged when someone else's edit got
there first), and a remote change is written to disk.

The engine does no networking of its own, and its document work happens in
one synchronous step per batch. Nothing awaits between reading a file's text
from the document and writing to it, so the pycrdt Provider running on the
same event loop cannot apply a remote update in between. Blob transfers are
the one thing that has to wait for the network. They run between steps
through a BlobStore, and the step that needed them is repeated afterwards.

Next to the document (``state.bin``) the engine keeps ``index.json``. Per
path it holds the file id, the content hash and the file's mtime, size and
inode. The hash says which shadows the saved document really holds, so a
restart never mistakes a remote change that was not written yet for a local
edit. The stat lets a scan skip every file that did not change, which matters
for paper folders with large data directories.

A checkout can be *held* (``.meertex/hold``, §10 Holding). Then step 1 does
not run at all, because whatever reaches the document also reaches everybody
through the sync handshake. Step 2 still writes other people's changes, around
the local ones, which stay as the difference between shadow and disk. On
release the next ordinary step sends them all.
"""

import hashlib
import os
import posixpath
import stat as stat_mod
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pycrdt import Doc

from . import ydoc as y
from .config import META_DIR, held_since, read_json, write_atomic, write_json
from .ignore import IGNORE_FILE, IgnoreRules

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_BLOB_BYTES = 50 * 1024 * 1024
# A file whose mtime is this close to the moment its stat was taken may have
# changed again within the same timestamp tick, without changing its size.
# Such a file is read again instead of trusted (git's "racily clean").
RACY_NS = 2_000_000_000
STATE_FILE = "state.bin"
INDEX_FILE = "index.json"
# One reconcile repeats its step after blob transfers. More rounds than this
# means the folder keeps changing under us, and the next batch carries on.
MAX_ROUNDS = 4


class BlobStore(Protocol):
    """Where blob contents travel (§11, Blobs). The server needs a blob before
    any entry points at it, so the engine uploads first and edits second."""

    async def upload(self, sha: str, data: bytes) -> None: ...

    async def download(self, sha: str) -> bytes: ...


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Stat:
    mtime_ns: int
    size: int
    ino: int

    @classmethod
    def of(cls, st: os.stat_result) -> "Stat":
        return cls(st.st_mtime_ns, st.st_size, st.st_ino)


@dataclass
class Content:
    """A file's content as the engine compares it, text or a blob by hash."""

    kind: str  # "text" or "blob"
    sha: str
    size: int
    text: str | None = None

    @classmethod
    def of_bytes(cls, data: bytes) -> "Content":
        if y.is_text(data, MAX_TEXT_BYTES):
            return cls("text", sha256(data), len(data), data.decode("utf-8"))
        return cls("blob", sha256(data), len(data))

    @classmethod
    def of_entry(cls, e: y.Entry) -> "Content":
        if e.kind == "text":
            data = (e.text or "").encode()
            return cls("text", sha256(data), len(data), e.text or "")
        return cls("blob", e.sha or "", e.size or 0)

    def same(self, other: "Content") -> bool:
        if self.kind != other.kind:
            return False
        if self.kind == "text" and self.text is not None and other.text is not None:
            return self.text == other.text
        return self.sha == other.sha


def matches(content: Content, e: y.Entry) -> bool:
    if content.kind != e.kind:
        return False
    if e.kind == "text":
        return content.text == (e.text or "")
    return content.sha == e.sha


@dataclass
class Shadow:
    id: str
    content: Content
    stat: Stat | None = None
    seen_ns: int = 0  # when ``stat`` was taken together with reading or writing the file


@dataclass
class Local:
    """What is on disk at a path. ``content`` None means there is no file."""

    content: Content | None
    stat: Stat | None
    changed: bool  # differs from the shadow, or exists without one
    seen_ns: int = 0


@dataclass
class Report:
    """What one reconcile did, for the log and for ``--json``."""

    sent: list[str] = field(default_factory=list)  # local edits that went into the document
    merged: list[str] = field(default_factory=list)  # ... merged with someone else's edit
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[list[str]] = field(default_factory=list)
    written: list[str] = field(default_factory=list)  # written to disk from the document
    removed: list[str] = field(default_factory=list)  # renamed by someone else, old path removed
    trashed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # written around a held local change
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending: bool = False  # blob transfers that have to wait for the server

    def empty(self) -> bool:
        return not any((self.sent, self.added, self.deleted, self.renamed, self.written,
                        self.removed, self.trashed, self.kept, self.warnings, self.errors))

    def as_dict(self) -> dict:
        return asdict(self)

    def extend(self, other: "Report") -> None:
        for key, value in asdict(other).items():
            if isinstance(value, list):
                getattr(self, key).extend(getattr(other, key))
        self.pending = other.pending


@dataclass
class _Todo:
    """What one synchronous step leaves for the next."""

    skip: set[str] = field(default_factory=set)  # paths left alone this step
    again: set[str] = field(default_factory=set)  # paths to run through another step
    uploads: dict[str, set[str]] = field(default_factory=dict)  # sha -> paths
    downloads: dict[str, set[str]] = field(default_factory=dict)  # sha -> paths
    changed: bool = False  # shadows or document changed, so state is saved
    reload_rules: bool = False


class Engine:
    def __init__(self, root: Path, doc: Doc | None = None, main_file: str = "main.tex",
                 blobs: BlobStore | None = None):
        self.root = root.resolve()
        self.meta = self.root / META_DIR
        self.doc = doc if doc is not None else Doc()
        self.blobs = blobs
        self.rules = IgnoreRules(self.root, main_file)
        self.shadows: dict[str, Shadow] = {}
        self.pending = False
        self.held = False
        self.held_paths: set[str] = set()  # local changes kept out of the document while held
        self._busy = False
        self._uploaded: set[str] = set()
        self._fetched: dict[str, Path] = {}  # downloaded blobs waiting to be written
        self._warned: set[str] = set()
        self._trash_stamp: str | None = None
        self._doc_shas: set[str] = set()
        mask = os.umask(0)
        os.umask(mask)
        self._file_mode = 0o666 & ~mask

    # --- State on disk -----------------------------------------------------------

    def load(self) -> None:
        """Read ``state.bin`` and ``index.json``. The shadow of each path is what
        the index says, with the text taken from the saved document."""
        state = self.meta / STATE_FILE
        if state.exists():
            try:
                self.doc.apply_update(state.read_bytes())
            except Exception as exc:  # pycrdt raises plain exceptions for a broken update
                raise RuntimeError(f"{state} cannot be read ({exc}). Check the project out into a new folder "
                                   "and copy your changes over.") from exc
        by_id = {e.id: e for e in y.entries(self.doc)}
        index = read_json(self.meta / INDEX_FILE)
        if index is None:
            # Without an index every entry is a shadow, and every file is read once.
            self.shadows = {p: Shadow(e.id, Content.of_entry(e)) for p, e in y.materialize(self.doc).items()}
            return
        self.shadows = {}
        for path, rec in (index.get("files") or {}).items():
            try:
                content = Content(rec["kind"], rec["sha"], int(rec["size"]), rec.get("text"))
                file_id = rec["id"]
            except (KeyError, TypeError, ValueError):
                continue
            if content.kind == "text" and content.text is None:
                e = by_id.get(file_id)
                if e is None or e.kind != "text":
                    continue
                from_doc = Content.of_entry(e)
                if from_doc.sha != content.sha:
                    continue  # no base text to merge against, so it counts as never synced
                content = from_doc
            st = None
            if "mtime_ns" in rec:
                st = Stat(int(rec["mtime_ns"]), int(rec["file_size"]), int(rec["ino"]))
            self.shadows[path] = Shadow(file_id, content, st, int(rec.get("seen_ns", 0)))

    def save(self) -> None:
        """Write the document and the shadows. A text shadow the saved document
        does not hold (a write that failed) is stored with its text."""
        self.meta.mkdir(parents=True, exist_ok=True)
        by_id = {e.id: e for e in y.entries(self.doc)}
        files = {}
        for path, s in sorted(self.shadows.items()):
            rec: dict = {"id": s.id, "kind": s.content.kind, "sha": s.content.sha, "size": s.content.size}
            if s.stat is not None:
                rec |= {"mtime_ns": s.stat.mtime_ns, "file_size": s.stat.size, "ino": s.stat.ino,
                        "seen_ns": s.seen_ns}
            if s.content.kind == "text":
                e = by_id.get(s.id)
                if e is None or e.kind != "text" or (e.text or "") != s.content.text:
                    rec["text"] = s.content.text
            files[path] = rec
        write_json(self.meta / INDEX_FILE, {"version": 1, "files": files})
        write_atomic(self.meta / STATE_FILE, self.doc.get_update())

    def clean_tmp(self) -> None:
        """Remove the engine's own leftovers from a run that was killed."""
        tmp = self.meta / "tmp"
        if tmp.is_dir():
            for leftover in tmp.iterdir():
                if leftover.is_file() and leftover.name.startswith(("w-", "blob-")):
                    leftover.unlink(missing_ok=True)

    # --- Watching the document ---------------------------------------------------

    def observe(self, remote: Callable[[], None], local: Callable[[], None] | None = None) -> None:
        """Call ``remote`` after every change to the files that the engine did
        not make itself, and ``local`` after each of its own. Both run inside
        pycrdt's commit, before the Provider can send anything, and must not
        touch the document."""

        def handler(_events) -> None:
            if not self._busy:
                remote()
            elif local is not None:
                local()

        # pycrdt keeps the subscription on the Map object. A temporary one would
        # be garbage collected and take the subscription with it.
        self._files = y.files_map(self.doc)
        self._subscription = self._files.observe_deep(handler)

    # --- Reconcile ---------------------------------------------------------------

    async def reconcile(self, paths: set[str] | None = None) -> Report:
        """Bring disk and document together. ``paths`` limits the local side to
        those paths (watcher events), None scans the whole folder. Remote
        changes are always handled in full."""
        out = Report()
        self._trash_stamp = None
        todo_paths = paths
        try:
            for _ in range(MAX_ROUNDS):
                todo = self._step(todo_paths, out)
                retry = set(todo.again)
                if todo.uploads or todo.downloads:
                    retry |= await self._transfer(todo, out)
                if not retry:
                    break
                todo_paths = retry
            else:
                out.pending = True
        finally:
            self._drop_fetched()
        self.pending = out.pending
        return out

    def _step(self, paths: set[str] | None, out: Report) -> _Todo:
        todo = _Todo()
        if self.rules.refresh() and self.rules.problem:
            self._warn_once(out, self.rules.problem)
        self.held = held_since(self.root) is not None
        full = paths is None
        self._busy = True
        try:
            with self.doc.transaction():
                view = y.materialize(self.doc)
                self._doc_shas = {e.sha for e in view.values() if e.kind == "blob" and e.sha}
                if paths is None:
                    paths = self.scan(out=out) | set(self.shadows) | set(view)
                else:
                    paths = self._expand(paths, out)
                paths |= self._remote_affected(view)
                locals_: dict[str, Local | None] = {}
                if not self.held:
                    edits = self._edit_count(out)
                    self._push(paths, view, locals_, todo, out)
                    todo.changed |= self._edit_count(out) != edits
            view = y.materialize(self.doc)
            targets = paths | self._remote_affected(view)
            if self.held:
                self._pull_held(targets, view, locals_, todo, out, full)
            else:
                self.held_paths.clear()
                self._pull(targets, view, locals_, todo, out)
        finally:
            self._busy = False
        if todo.reload_rules and self.rules.refresh() and self.rules.problem:
            self._warn_once(out, self.rules.problem)
        if todo.changed:
            self.save()
        return todo

    @staticmethod
    def _edit_count(out: Report) -> int:
        return len(out.sent) + len(out.added) + len(out.deleted) + len(out.renamed)

    def _remote_affected(self, view: dict[str, y.Entry]) -> set[str]:
        """Paths where the document no longer says what the shadow says."""
        out = {p for p, s in self.shadows.items()
               if (e := view.get(p)) is None or e.id != s.id or not matches(s.content, e)}
        out.update(p for p in view if p not in self.shadows)
        return out

    # Step 1 takes local changes into the document.

    def _push(self, paths: set[str], view: dict[str, y.Entry], locals_: dict[str, Local | None],
              todo: _Todo, out: Report) -> None:
        deletions: dict[str, Shadow] = {}
        creations: dict[str, Content] = {}
        for p in sorted(paths):
            local = self._read(p, out)
            locals_[p] = local
            if local is None:
                todo.skip.add(p)
                continue
            if not local.changed or self.ignored_path(p):
                continue
            base = self.shadows.get(p)
            if base is None:
                e = view.get(p)
                if local.content is not None and not (e is not None and matches(local.content, e)):
                    creations[p] = local.content
                continue
            cur = y.get_entry(self.doc, base.id)
            if local.content is None:
                if cur is not None and matches(base.content, cur):
                    deletions[p] = base
                # Otherwise someone edited the file since, which beats our delete.
                continue
            self._push_change(p, base, cur, local.content, todo, out)

        # A delete and a create with the same content in one batch is a rename.
        pool: dict[tuple[str, str], list[str]] = {}
        for p, base in deletions.items():
            pool.setdefault((base.content.kind, base.content.sha), []).append(p)
        for q in sorted(creations):
            olds = pool.get((creations[q].kind, creations[q].sha))
            if not olds:
                continue
            p = min(olds, key=lambda o: (posixpath.basename(o) != posixpath.basename(q), o))
            olds.remove(p)
            y.rename(self.doc, deletions.pop(p).id, q)
            del creations[q]
            out.renamed.append([p, q])
        for p, base in sorted(deletions.items()):
            y.remove(self.doc, base.id)
            out.deleted.append(p)
        for q, content in sorted(creations.items()):
            if content.kind == "blob" and not self._have_blob(content.sha):
                todo.uploads.setdefault(content.sha, set()).add(q)
                todo.skip.add(q)
                continue
            self._add(q, content)
            out.added.append(q)

    def _push_change(self, p: str, base: Shadow, cur: y.Entry | None, content: Content,
                     todo: _Todo, out: Report) -> None:
        if content.kind == "blob" and not self._have_blob(content.sha):
            todo.uploads.setdefault(content.sha, set()).add(p)
            todo.skip.add(p)
            return
        if cur is None:
            # Removed by someone else while we changed it. Ours wins, as a new entry.
            self._add(p, content)
            out.added.append(p)
            return
        if cur.kind == content.kind == "text":
            if (cur.text or "") == base.content.text:
                y.write_text(self.doc, cur.id, content.text or "")
            else:
                merged = y.merge_text(base.content.text or "", content.text or "", cur.text or "")
                y.write_text(self.doc, cur.id, merged)
                out.merged.append(p)
        elif cur.kind == content.kind == "blob":
            y.write_blob(self.doc, cur.id, content.sha, content.size)
        else:
            # Text became binary or the other way round. It gets a new entry on the same path.
            y.remove(self.doc, cur.id)
            self._add(p, content)
        out.sent.append(p)

    def _add(self, path: str, content: Content) -> str:
        if content.kind == "text":
            return y.add_text(self.doc, path, content.text or "")
        return y.add_blob(self.doc, path, content.sha, content.size)

    def _have_blob(self, sha: str) -> bool:
        return sha in self._uploaded or sha in self._doc_shas

    # Step 2 writes the document onto the disk.

    def _pull(self, targets: set[str], view: dict[str, y.Entry], locals_: dict[str, Local | None],
              todo: _Todo, out: Report) -> None:
        writes: list[tuple[str, y.Entry]] = []
        removals: list[tuple[str, bool]] = []
        for p in sorted(targets):
            if p in todo.skip:
                continue
            e = view.get(p)
            base = self.shadows.get(p)
            if p in locals_:
                local = locals_[p]
            else:
                local = locals_[p] = self._read(p, out)
                if local is not None and local.changed and local.content is not None \
                        and not self.ignored_path(p):
                    todo.again.add(p)  # a local change that step 1 did not see, for the next step
                    continue
            if local is None:
                continue
            if e is None:
                if base is None:
                    continue
                if local.content is None:
                    self._drop_shadow(p, todo)
                    continue
                moved = y.get_entry(self.doc, base.id) is not None
                if local.changed and (self.ignored_path(p) or not moved):
                    self._drop_shadow(p, todo)  # keep the user's file, it is theirs now
                    continue
                removals.append((p, moved))
                continue
            if local.content is not None and matches(local.content, e):
                self._set_shadow(p, e, local.content, local.stat, local.seen_ns, todo)
                continue
            if self.ignored_path(p) and base is not None and base.id == e.id and matches(base.content, e):
                continue  # nothing new from the others, and a local change to an ignored file stays local
            writes.append((p, e))
        for p, e in writes:  # before removals, so a renamed blob can be copied from its old path
            self._write(p, e, todo, out)
        for p, moved in removals:
            self._remove(p, moved, todo, out)

    # Step 2 while held: the document onto the disk, around the local changes.

    def _pull_held(self, targets: set[str], view: dict[str, y.Entry], locals_: dict[str, Local | None],
                   todo: _Todo, out: Report, full: bool) -> None:
        """A local change stays the difference between shadow and disk. A file
        nobody touched here is written as usual. A file changed here and by
        someone else gets the merge on disk and the document's text as its
        shadow, so the difference is still exactly the local change."""
        where = {e.id: path for path, e in view.items()}
        held: set[str] = set()  # paths with a held change after this step
        owned: set[str] = set()  # paths the ordinary step 2 must leave alone
        claimed: set[str] = set()  # entries that a held change belongs to
        moves: list[tuple[str, str, y.Entry, bytes | None]] = []
        for p in sorted(targets):
            if p in todo.skip:
                continue
            local = self._local(p, locals_, out)
            if local is None or not local.changed or self.ignored_path(p):
                continue
            base = self.shadows.get(p)
            if base is None:
                e = view.get(p)
                if local.content is None or (e is not None and matches(local.content, e)):
                    continue  # nothing here, or the same file came from someone else
                owned.add(p)
                held.add(p)
                if e is not None:
                    self._warn_once(out, f"{p} is new here and from someone else. Yours stays until release.")
                continue
            cur = y.get_entry(self.doc, base.id)
            if local.content is None:
                if cur is not None and matches(base.content, cur):
                    owned.add(p)  # stays deleted, also when someone renamed it
                    held.add(p)
                    claimed.add(cur.id)
                # Otherwise someone edited it since, which beats the delete.
                continue
            owned.add(p)
            if cur is None or cur.id not in where:
                # Deleted by someone else (or hidden by a path clash). The file stays
                # and goes out as a new one on release.
                if cur is None:
                    self._drop_shadow(p, todo)
                held.add(p)
                continue
            claimed.add(cur.id)
            target = where[cur.id]
            held.add(target)
            if matches(base.content, cur):
                if target != p:  # renamed by someone else
                    data = (local.content.text or "").encode() if local.content.kind == "text" else None
                    moves.append((p, target, cur, data))
                continue  # nobody else changed it, so it stays as it is
            if cur.kind == local.content.kind == "text":
                merged = y.merge_text(base.content.text or "", local.content.text or "", cur.text or "")
                same = target == p and merged == local.content.text
                moves.append((p, target, cur, None if same else merged.encode()))
            else:
                moves.append((p, target, cur, None))  # a blob change stays as it is
        owned |= {where[i] for i in claimed}
        for p, q, cur, data in moves:
            self._move_held(p, q, cur, data, todo, out)
        self._pull({p for p in targets if p not in owned}, view, locals_, todo, out)
        if full:
            self.held_paths = held
        else:
            self.held_paths = (self.held_paths - targets) | held

    def _move_held(self, p: str, q: str, cur: y.Entry, data: bytes | None, todo: _Todo, out: Report) -> None:
        """Put a held change where the document has its file now: ``data`` (the
        merge) at ``q``, or the local file moved there. The shadow becomes the
        document's content without a stat, so the file is read again each time."""
        if data is not None:
            if self._put(q, data, None, out) is None:
                return
            if q != p:
                (self.root / p).unlink(missing_ok=True)
        elif q != p:
            try:
                (self.root / q).parent.mkdir(parents=True, exist_ok=True)
                os.replace(self.root / p, self.root / q)
            except OSError as exc:
                self._warn_once(out, f"Cannot move {p} to {q} ({exc.strerror or exc})", error=True)
                return
        if q != p:
            self._drop_shadow(p, todo)
            self._prune((self.root / p).parent)
        self.shadows[q] = Shadow(cur.id, Content.of_entry(cur))
        todo.changed = True
        if data is not None or q != p:
            out.kept.append(q)

    def _local(self, p: str, locals_: dict[str, Local | None], out: Report) -> Local | None:
        if p not in locals_:
            locals_[p] = self._read(p, out)
        return locals_[p]

    def local_changes(self) -> list[str]:
        """The paths whose content on disk the document does not have, found
        without changing either. ``meertex status`` counts held changes with it."""
        out = Report()
        view = y.materialize(self.doc)
        changed = []
        for p in sorted(self.scan() | set(self.shadows)):
            if self.ignored_path(p):
                continue
            local = self._read(p, out)
            if local is None or not local.changed:
                continue
            if p not in self.shadows and local.content is not None and p in view and matches(local.content, view[p]):
                continue
            changed.append(p)
        return changed

    def _write(self, p: str, e: y.Entry, todo: _Todo, out: Report) -> None:
        source: Path | None = None
        if e.kind == "blob":
            source = self._blob_source(e.sha or "")
            if source is None:
                todo.downloads.setdefault(e.sha or "", set()).add(p)
                return
        st = self._put(p, (e.text or "").encode() if source is None else None, source, out)
        if st is None:
            return
        self._set_shadow(p, e, Content.of_entry(e), Stat.of(st), time.time_ns(), todo)
        (out.conflicts if p != e.path else out.written).append(p)
        if p == IGNORE_FILE:
            todo.reload_rules = True

    def _put(self, p: str, data: bytes | None, source: Path | None, out: Report) -> os.stat_result | None:
        """Write ``data`` (or a copy of ``source``) to ``p`` atomically, keeping
        the file's mode. Returns the new stat, or None when it failed."""
        full = self.root / p
        if not self._inside(full.parent):
            self._warn_once(out, f"{p} is not written because a folder on its path leads outside the checkout")
            return None
        tmp_dir = self.meta / "tmp"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            full.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix="w-")
            try:
                with os.fdopen(fd, "wb") as f:
                    if source is None:
                        f.write(data or b"")
                    else:
                        with open(source, "rb") as src:
                            while chunk := src.read(1 << 20):
                                f.write(chunk)
                try:
                    mode = stat_mod.S_IMODE(os.stat(full).st_mode)
                except FileNotFoundError:
                    mode = self._file_mode
                os.chmod(tmp, mode)
                os.replace(tmp, full)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            return os.stat(full)
        except OSError as exc:
            self._warn_once(out, f"Cannot write {p} ({exc.strerror or exc})", error=True)
            return None

    def _remove(self, p: str, moved: bool, todo: _Todo, out: Report) -> None:
        full = self.root / p
        try:
            if moved:
                full.unlink()
                out.removed.append(p)
            else:
                dest = self._trash_dir() / p
                dest.parent.mkdir(parents=True, exist_ok=True)
                n = 1
                while dest.exists():
                    n += 1
                    dest = dest.with_name(f"{Path(p).name}.{n}")
                os.replace(full, dest)
                out.trashed.append(p)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._warn_once(out, f"Cannot remove {p} ({exc.strerror or exc})", error=True)
            return
        self._drop_shadow(p, todo)
        if p == IGNORE_FILE:
            todo.reload_rules = True
        self._prune(full.parent)

    def _prune(self, parent: Path) -> None:
        """Remove folders that a removal left empty, up to the checkout's root."""
        while parent != self.root and self._inside(parent):
            try:
                parent.rmdir()  # only succeeds when it is empty
            except OSError:
                break
            parent = parent.parent

    def _trash_dir(self) -> Path:
        if self._trash_stamp is None:
            self._trash_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.meta / "trash" / self._trash_stamp

    def _set_shadow(self, p: str, e: y.Entry, content: Content, st: Stat | None, seen_ns: int,
                    todo: _Todo) -> None:
        new = Shadow(e.id, content, st, seen_ns)
        if self.shadows.get(p) != new:
            self.shadows[p] = new
            todo.changed = True
        if p == IGNORE_FILE:
            todo.reload_rules = True

    def _drop_shadow(self, p: str, todo: _Todo) -> None:
        if self.shadows.pop(p, None) is not None:
            todo.changed = True

    # --- Blobs -------------------------------------------------------------------

    def _blob_source(self, sha: str) -> Path | None:
        """A file that already holds this blob. That is a download, or a synced
        file with that content whose stat says it is unchanged."""
        if sha in self._fetched:
            return self._fetched[sha]
        for p, s in self.shadows.items():
            if s.content.kind == "blob" and s.content.sha == sha and s.stat is not None:
                try:
                    if Stat.of(os.stat(self.root / p)) == s.stat:
                        return self.root / p
                except OSError:
                    continue
        return None

    async def _transfer(self, todo: _Todo, out: Report) -> set[str]:
        """Upload and download what the last step needs. Returns the paths to
        run through another step."""
        retry: set[str] = set()
        if self.blobs is None:
            out.pending = True
            return retry
        for sha, paths in todo.uploads.items():
            # Files with the same content share one upload, which any of them can feed.
            for p in sorted(paths):
                try:
                    data = (self.root / p).read_bytes()
                except OSError:
                    continue
                if sha256(data) == sha:
                    break
            else:
                retry |= paths  # changed again meanwhile, the next step reads them anew
                continue
            try:
                await self.blobs.upload(sha, data)
            except Exception as exc:  # noqa: BLE001 - network or server, retried later
                out.pending = True
                self._warn_once(out, f"Cannot upload {', '.join(sorted(paths))} yet ({exc})")
                continue
            self._uploaded.add(sha)
            retry |= paths
        for sha, paths in todo.downloads.items():
            try:
                data = await self.blobs.download(sha)
            except Exception as exc:  # noqa: BLE001 - network or server, retried later
                out.pending = True
                self._warn_once(out, f"Cannot download {', '.join(sorted(paths))} yet ({exc})")
                continue
            if sha256(data) != sha:
                out.pending = True
                self._warn_once(out, f"The server sent the wrong content for {', '.join(sorted(paths))}",
                                error=True)
                continue
            tmp_dir = self.meta / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            target = tmp_dir / f"blob-{sha}"
            target.write_bytes(data)
            self._fetched[sha] = target
            retry |= paths
        return retry

    def _drop_fetched(self) -> None:
        for path in self._fetched.values():
            path.unlink(missing_ok=True)
        self._fetched.clear()

    # --- The disk ----------------------------------------------------------------

    def _inside(self, path: Path) -> bool:
        real = os.path.realpath(path)
        return real == str(self.root) or real.startswith(str(self.root) + os.sep)

    def _usable(self, rel: str, out: Report | None) -> bool:
        try:
            problem = y.path_problem(rel)
        except UnicodeEncodeError:
            problem = "the name is not valid UTF-8"
        if problem and out is not None:
            self._warn_once(out, f"{rel} is not synced ({problem})")
        return problem is None

    def scan(self, sub: str = "", out: Report | None = None) -> set[str]:
        """Every file under ``sub`` that is synced, as POSIX paths. Ignored
        folders are pruned, so the walk never looks inside them."""
        found: set[str] = set()
        top = self.root / sub if sub else self.root
        for dirpath, dirnames, filenames in os.walk(top):
            rel_dir = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            keep = []
            for d in dirnames:
                rel = f"{rel_dir}/{d}" if rel_dir else d
                if rel != META_DIR and not self.rules.ignored_dir(rel):
                    keep.append(d)
            dirnames[:] = sorted(keep)
            for name in filenames:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if not self.rules.ignored(rel) and self._usable(rel, out):
                    found.add(rel)
        return found

    def _expand(self, paths: set[str], out: Report) -> set[str]:
        """Watcher paths to file paths. A folder stands for the files in it now
        and for the synced files that were in it."""
        found: set[str] = set()
        for p in paths:
            p = p.strip("/")
            if not p or p == META_DIR or p.startswith(META_DIR + "/"):
                continue
            full = self.root / p
            if full.is_dir() and not full.is_symlink():
                if not self.ignored_path(p, is_dir=True):
                    found |= self.scan(p, out)
            elif self._usable(p, None):
                found.add(p)
            prefix = p + "/"
            found.update(s for s in self.shadows if s.startswith(prefix))
        return found

    def ignored_path(self, rel: str, is_dir: bool = False) -> bool:
        """Whether ``rel`` or any folder above it is ignored."""
        parts = rel.split("/")
        for i in range(1, len(parts)):
            if self.rules.ignored_dir("/".join(parts[:i])):
                return True
        return self.rules.ignored_dir(rel) if is_dir else self.rules.ignored(rel)

    def _read(self, p: str, out: Report) -> Local | None:
        """The file at ``p``, or None when it cannot be synced right now (too
        large, unreadable). Unchanged stats skip the read."""
        base = self.shadows.get(p)
        full = self.root / p
        try:
            st = os.stat(full)
        except (FileNotFoundError, NotADirectoryError):
            return Local(None, None, changed=base is not None)
        except OSError as exc:
            self._warn_once(out, f"Cannot read {p} ({exc.strerror or exc})")
            return None
        if not stat_mod.S_ISREG(st.st_mode):
            return Local(None, None, changed=base is not None)
        fst = Stat.of(st)
        if base is not None and base.stat == fst and base.seen_ns - fst.mtime_ns > RACY_NS:
            return Local(base.content, fst, changed=False, seen_ns=base.seen_ns)
        if fst.size > MAX_BLOB_BYTES:
            self._warn_once(out, f"{p} is larger than 50 MB and is not synced")
            return None
        seen = time.time_ns()
        try:
            data = full.read_bytes()
        except OSError as exc:
            self._warn_once(out, f"Cannot read {p} ({exc.strerror or exc})")
            return None
        content = Content.of_bytes(data)
        changed = base is None or not content.same(base.content)
        return Local(content, fst, changed=changed, seen_ns=seen)

    def _warn_once(self, out: Report, message: str, error: bool = False) -> None:
        if message in self._warned:
            return
        self._warned.add(message)
        (out.errors if error else out.warnings).append(message)
