"""The project document's structure and the edits on it (docs/DESIGN.md §8).

Shared by the server and the CLI. The canonical file is ``app/live/ydoc.py``,
``cli/meertex/ydoc.py`` is a byte-for-byte copy, and tests/test_ydoc_shared.py
fails when they drift. It depends on pycrdt and diff-match-patch only, because
the CLI cannot import the server.

Two things here are easy to get wrong elsewhere, which is why they live in one
place:

* pycrdt counts text positions in UTF-8 bytes. Every edit converts from Python
  string indexes, and an edit that splits a multibyte character corrupts the
  text (pycrdt then inserts at the end instead).
* Yjs, and so the browser, counts text positions in UTF-16 code units.
  ``utf16_index`` and ``from_utf16_index`` convert. Do not use pycrdt's
  StickyIndex for anchors: once the anchored text was split by an edit, its
  get_index() adds byte lengths and UTF-16 lengths, so it drifts on any
  non-ASCII text. Review comments are anchored by diffing instead (§13).
* Two entries can carry the same path. Everybody must pick the same winner, or
  the server's history and a checkout would disagree about which file is which.
"""

import hashlib
import posixpath
import uuid
from dataclasses import dataclass

from diff_match_patch import diff_match_patch
from pycrdt import Doc, Map, Text

FILES = "files"
MAX_PATH_BYTES = 1024
# Diffing gives up and falls back to a coarser diff after this long. A coarse
# diff is still correct, it just merges less finely with concurrent edits.
DIFF_TIMEOUT_SECONDS = 2.0


@dataclass
class Entry:
    id: str
    path: str
    kind: str  # "text" or "blob"
    text: str | None = None
    sha: str | None = None
    size: int | None = None

    def content_sha(self) -> str:
        """SHA-256 of the content, for text and blobs alike."""
        if self.kind == "text":
            return hashlib.sha256((self.text or "").encode()).hexdigest()
        return self.sha or ""


def files_map(doc: Doc) -> Map:
    return doc.get(FILES, type=Map)


def new_file_id() -> str:
    return uuid.uuid4().hex


# --- Paths ---------------------------------------------------------------------


def path_problem(path: str) -> str | None:
    """Why ``path`` cannot name a file in a project, or None when it can."""
    if not path:
        return "The path is empty"
    if len(path.encode()) > MAX_PATH_BYTES:
        return "The path is longer than 1024 bytes"
    if "\\" in path or "\x00" in path:
        return "The path contains a backslash or a NUL character"
    if path.startswith("/"):
        return "The path must be relative"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "The path has an empty, '.' or '..' segment"
    if parts[0] == ".meertex" or parts[0] == ".git":
        return f"{parts[0]}/ is reserved"
    return None


def conflict_path(path: str, file_id: str) -> str:
    """Where a losing entry of a path clash is written: ``intro.conflict-1a2b3c4d.tex``."""
    head, name = posixpath.split(path)
    stem, dot, suffix = name.partition(".")
    if not stem:  # ".gitignore": the whole name is the stem
        stem, dot, suffix = name, "", ""
    renamed = f"{stem}.conflict-{file_id[:8]}{dot}{suffix}"
    return posixpath.join(head, renamed) if head else renamed


def is_text(data: bytes, max_text_bytes: int) -> bool:
    if len(data) > max_text_bytes or b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# --- Reading -------------------------------------------------------------------


def _entry(file_id: str, value: Map) -> Entry | None:
    try:
        path = value["path"]
        kind = value["kind"]
    except KeyError:
        return None
    if not isinstance(path, str) or kind not in ("text", "blob"):
        return None
    if kind == "text":
        text = value.get("text")
        return Entry(id=file_id, path=path, kind="text", text=str(text) if text is not None else "")
    sha = value.get("sha")
    # Yjs numbers are doubles, so a size comes back as 1234.0.
    size = value.get("size")
    return Entry(id=file_id, path=path, kind="blob", sha=sha, size=int(size) if size is not None else None)


def entries(doc: Doc) -> list[Entry]:
    """Every well-formed entry, in id order. Malformed ones are skipped."""
    files = files_map(doc)
    out = []
    for file_id in sorted(files.keys()):
        value = files[file_id]
        if isinstance(value, Map):
            e = _entry(file_id, value)
            if e is not None and path_problem(e.path) is None:
                out.append(e)
    return out


def materialize(doc: Doc) -> dict[str, Entry]:
    """The files as they are written to disk, by path. On a path clash the entry
    with the lowest id keeps the path and every other one goes to its
    conflict_path(). A conflict path that is itself taken is skipped, which only
    happens when someone names a file like a conflict copy on purpose."""
    by_path: dict[str, Entry] = {}
    losers: list[Entry] = []
    for e in entries(doc):  # id order, so the first one seen wins
        if e.path in by_path:
            losers.append(e)
        else:
            by_path[e.path] = e
    for e in losers:
        target = conflict_path(e.path, e.id)
        if target not in by_path:
            by_path[target] = e
    return by_path


def find(doc: Doc, path: str) -> Entry | None:
    """The entry that is written to ``path``."""
    return materialize(doc).get(path)


def get_entry(doc: Doc, file_id: str) -> Entry | None:
    files = files_map(doc)
    if file_id not in files:
        return None
    value = files[file_id]
    return _entry(file_id, value) if isinstance(value, Map) else None


# --- Writing -------------------------------------------------------------------


def byte_offset(s: str, index: int) -> int:
    """pycrdt's position for the Python string index ``index`` of ``s``."""
    return len(s[:index].encode())


def utf16_index(s: str, index: int) -> int:
    """The UTF-16 position of the Python string index ``index`` in ``s``."""
    return len(s[:index].encode("utf-16-le")) // 2


def from_utf16_index(s: str, index16: int) -> int:
    """The Python string index of the UTF-16 position ``index16`` in ``s``."""
    return len(s.encode("utf-16-le")[: index16 * 2].decode("utf-16-le", errors="ignore"))


def _diff(old: str, new: str) -> list[tuple[int, str]]:
    dmp = diff_match_patch()
    dmp.Diff_Timeout = DIFF_TIMEOUT_SECONDS
    return dmp.diff_main(old, new)


def set_text(ytext: Text, new: str) -> bool:
    """Turn ``ytext`` into ``new`` with the smallest character edits, so that
    concurrent edits elsewhere in the same text survive. Returns whether
    anything changed. The caller holds the transaction if several edits should
    go out as one update."""
    old = str(ytext)
    if old == new:
        return False
    pos = 0  # in bytes, moving forward through the text as it is being edited
    with ytext.doc.transaction():
        for op, chunk in _diff(old, new):
            size = len(chunk.encode())
            if op == 0:
                pos += size
            elif op == -1:
                del ytext[pos : pos + size]
            else:
                ytext.insert(pos, chunk)
                pos += size
    return True


def merge_text(base: str, ours: str, theirs: str) -> str:
    """Our change from ``base`` to ``ours``, replayed onto ``theirs``.

    The sync client needs this when someone else's edit reached the document
    between our last write of a file and the next local save. A hunk that no
    longer applies is dropped, the same trade git makes with a conflict, except
    that nothing is left in the text for LaTeX to choke on."""
    if base == ours:
        return theirs
    if base == theirs:
        return ours
    dmp = diff_match_patch()
    dmp.Diff_Timeout = DIFF_TIMEOUT_SECONDS
    patches = dmp.patch_make(base, ours)
    merged, _applied = dmp.patch_apply(patches, theirs)
    return merged


def add_text(doc: Doc, path: str, text: str, file_id: str | None = None) -> str:
    file_id = file_id or new_file_id()
    files_map(doc)[file_id] = Map({"path": path, "kind": "text", "text": Text(text)})
    return file_id


def add_blob(doc: Doc, path: str, sha: str, size: int, file_id: str | None = None) -> str:
    file_id = file_id or new_file_id()
    files_map(doc)[file_id] = Map({"path": path, "kind": "blob", "sha": sha, "size": size})
    return file_id


def write_text(doc: Doc, file_id: str, text: str) -> bool:
    entry = files_map(doc)[file_id]
    return set_text(entry["text"], text)


def write_blob(doc: Doc, file_id: str, sha: str, size: int) -> bool:
    entry = files_map(doc)[file_id]
    if entry["sha"] == sha:
        return False
    with doc.transaction():
        entry["sha"] = sha
        entry["size"] = size
    return True


def rename(doc: Doc, file_id: str, path: str) -> None:
    files_map(doc)[file_id]["path"] = path


def remove(doc: Doc, file_id: str) -> None:
    files = files_map(doc)
    if file_id in files:
        del files[file_id]


def put_text(doc: Doc, path: str, text: str) -> str:
    """Write ``text`` to ``path``: a diff when a text file is there, otherwise a
    new entry (replacing a blob of that path). Returns the file id."""
    with doc.transaction():
        current = find(doc, path)
        if current is not None and current.kind == "text":
            write_text(doc, current.id, text)
            return current.id
        if current is not None:
            remove(doc, current.id)
        return add_text(doc, path, text)


def put_blob(doc: Doc, path: str, sha: str, size: int) -> str:
    """Point ``path`` at a stored blob, replacing a text file of that path."""
    with doc.transaction():
        current = find(doc, path)
        if current is not None and current.kind == "blob":
            write_blob(doc, current.id, sha, size)
            return current.id
        if current is not None:
            remove(doc, current.id)
        return add_blob(doc, path, sha, size)
