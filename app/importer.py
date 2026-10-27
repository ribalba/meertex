"""Starting a paper from a template zip, such as a publisher's author kit
(docs/DESIGN.md §4, Starting files).

Publishers ship their LaTeX templates as zips: a class file, a sample paper,
a bibliography, figures. Importing one writes every file into the project's
live document, so the editor, the history and every checkout get them like
any other edit.

A zip is untrusted. Nothing is ever extracted to disk: each member is read
into memory with a size cap, and its name becomes a document path only after
the same checks every path passes (ydoc.path_problem). Links, encrypted
members and names that climb out of the archive are skipped.
"""

import io
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass, field

from . import live
from .config import get_settings
from .live import blobs, rooms, ydoc

MAX_FILES = 2000
MAX_TOTAL_BYTES = 200 * 1024 * 1024

# Never imported: what operating systems leave in archives, version control,
# and the build outputs a LaTeX run leaves behind (the sync client ignores
# them too, docs/DESIGN.md §10).
JUNK_PARTS = {"__MACOSX", ".git", ".svn", ".DS_Store", "Thumbs.db", "desktop.ini"}
JUNK_SUFFIXES = (
    ".aux", ".bcf", ".blg", ".dvi", ".fdb_latexmk", ".fls", ".lof", ".log", ".lot",
    ".nav", ".out", ".run.xml", ".snm", ".synctex.gz", ".toc", ".vrb", ".xdv",
)

_DOCUMENTCLASS = re.compile(r"^[ \t]*\\documentclass\b", re.MULTILINE)
_BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{document\}")
# Names that usually mean "this is the paper", best first.
_MAIN_NAMES = ("main.tex", "paper.tex", "article.tex", "manuscript.tex")


class ImportRefused(ValueError):
    """The zip cannot be imported. ``args[0]`` says why."""


@dataclass
class Imported:
    files: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    main_file: str | None = None
    main_candidates: list[str] = field(default_factory=list)


def _skip_reason(name: str, info: zipfile.ZipInfo) -> str | None:
    parts = name.split("/")
    if any(p in JUNK_PARTS for p in parts) or parts[-1].startswith("._"):
        return "not part of the template"
    if name.endswith(JUNK_SUFFIXES):
        return "a build output"
    if info.flag_bits & 0x1:
        return "encrypted"
    if stat.S_ISLNK(info.external_attr >> 16):
        return "a link"
    return None


def _clean(name: str) -> str | None:
    """The member's name as a relative path, or None when it climbs out."""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    norm = posixpath.normpath(name)
    if norm in (".", "") or norm == ".." or norm.startswith("../"):
        return None
    return norm


def _strip_common_folder(paths: list[str]) -> dict[str, str]:
    """Map each path to itself without the folder every file sits in. Kits
    often wrap everything in one or two folders ("acmart-primary/...")."""
    mapping = {p: p for p in paths}
    while mapping and all("/" in p for p in mapping.values()):
        tops = {p.split("/", 1)[0] for p in mapping.values()}
        if len(tops) != 1:
            break
        mapping = {orig: p.split("/", 1)[1] for orig, p in mapping.items()}
    return mapping


def guess_main(texts: dict[str, str]) -> tuple[str | None, list[str]]:
    """The file that is most likely the paper, and every candidate. A
    candidate has \\documentclass and \\begin{document}. Fewer folders win,
    then a usual name, then a file that is not a class's own documentation."""
    candidates = [
        p for p, t in texts.items()
        if p.endswith(".tex") and _DOCUMENTCLASS.search(t) and _BEGIN_DOCUMENT.search(t)
    ]

    def rank(p: str) -> tuple:
        name = posixpath.basename(p).lower()
        usual = _MAIN_NAMES.index(name) if name in _MAIN_NAMES else len(_MAIN_NAMES)
        looks_like_docs = any(w in name for w in ("doc", "manual", "guide", "readme", "instructions"))
        return (p.count("/"), usual, looks_like_docs, len(p), p)

    candidates.sort(key=rank)
    return (candidates[0] if candidates else None), candidates


def import_zip(project_id: str, data: bytes, editor: str, replace: bool = False) -> Imported:
    """Write the files of the zip ``data`` into the project's live document.
    Raises ImportRefused when the zip is unusable, or when files exist already
    and ``replace`` is false."""
    settings = get_settings()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportRefused("That is not a zip file") from exc

    out = Imported()
    members: list[tuple[str, zipfile.ZipInfo]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = _clean(info.filename)
        if name is None:
            out.skipped.append({"path": info.filename, "reason": "its name leads outside the zip"})
            continue
        reason = _skip_reason(name, info)
        if reason:
            out.skipped.append({"path": name, "reason": reason})
            continue
        members.append((name, info))
    if not members:
        raise ImportRefused("The zip has no files to import")
    if len(members) > MAX_FILES:
        raise ImportRefused(f"The zip has more than {MAX_FILES} files")

    mapping = _strip_common_folder([name for name, _info in members])
    contents: dict[str, bytes] = {}
    total = 0
    for name, info in members:
        path = mapping[name]
        problem = ydoc.path_problem(path)
        if problem:
            out.skipped.append({"path": path, "reason": problem})
            continue
        if info.file_size > settings.max_blob_bytes:
            out.skipped.append({"path": path, "reason": "larger than one file may be"})
            continue
        # The header's size can lie (a zip bomb). Read at most one byte more
        # than it says, and stop counting at the overall cap.
        with archive.open(info) as f:
            body = f.read(info.file_size + 1)
        if len(body) != info.file_size:
            raise ImportRefused(f"{path} is damaged in the zip")
        total += len(body)
        if total > MAX_TOTAL_BYTES:
            raise ImportRefused("The zip unpacks to more than 200 MB")
        contents[path] = body

    texts = {p: b.decode() for p, b in contents.items() if ydoc.is_text(b, settings.max_text_bytes)}
    out.main_file, out.main_candidates = guess_main(texts)

    # Blobs first: an entry must never point at bytes that are not stored.
    shas = {p: blobs.store(project_id, b) for p, b in contents.items() if p not in texts}
    with rooms.get(project_id).edit(editor) as doc, doc.transaction():
        present = ydoc.materialize(doc)
        clashes = sorted(p for p in contents if p in present)
        if clashes and not replace:
            shown = ", ".join(clashes[:5]) + (" and more" if len(clashes) > 5 else "")
            raise ImportRefused(f"These files exist already: {shown}. Import with replace to write over them.")
        for path in sorted(contents):
            if path in texts:
                ydoc.put_text(doc, path, texts[path])
                out.files.append({"path": path, "kind": "text", "size": len(contents[path])})
            else:
                ydoc.put_blob(doc, path, shas[path], len(contents[path]))
                out.files.append({"path": path, "kind": "blob", "size": len(contents[path])})
    live.snapshot(project_id, editors=[editor])
    return out
