"""Which .bib files a check reads and which .tex files its \\cite keys come
from (docs/DESIGN.md §16, Files).

A project can hold more than one paper. The submitted version kept in a
folder of its own with a copy of the .bib file is the usual case. Every
document (a .tex file with ``\\documentclass``, and the main file) is read
with the files it includes, and its ``\\bibliography`` and ``\\addbibresource``
names are found as bibtex and biber find them, relative to the document's
folder. A .bib file is then known by the lines that load it.

The project chooses what the check reads (``bib_mode``):

- ``main``, the default, reads the .bib files the main file loads, itself or
  through the files it includes. When it loads none, every .bib file is read.
- ``all`` reads every .bib file.
- ``chosen`` reads the files in ``bib_files`` that are still there.

The \\cite keys come from the documents that read a checked .bib file or no
.bib file at all, and from the .tex files no document includes. So the
copy's citations neither count as uses nor become missing keys.
"""

import posixpath
from dataclasses import dataclass, field

from . import latex

MODES = ("main", "all", "chosen")
TEX_EXTENSIONS = (".tex", ".ltx")


def is_bib(path: str) -> bool:
    return path.lower().endswith(".bib")


def is_tex(path: str) -> bool:
    return path.lower().endswith(TEX_EXTENSIONS)


@dataclass
class Bib:
    path: str
    # (path, line) of every command that loads it.
    loaded_by: list[tuple[str, int]] = field(default_factory=list)
    # The main file loads it, itself or through a file it includes.
    main: bool = False
    checked: bool = False


@dataclass
class Scope:
    mode: str
    bibs: list[Bib]
    # The .bib files the check reads, in path order.
    checked: list[str]
    # The .tex files whose \cite keys count, in path order.
    tex: list[str]
    # ``main`` and the main file loads no .bib file, so every one is read.
    fallback: bool = False


def _join(folder: str, name: str) -> str | None:
    """``name`` in ``folder`` as a project path, or None outside the project."""
    if name.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(folder, name) if folder else name)
    if path == "." or path == ".." or path.startswith("../"):
        return None
    return path


def _find(name: str, folders: list[str], files: set[str], extensions: tuple[str, ...]) -> str | None:
    """The first of ``folders`` that has ``name``, with its extension added
    when it has none of ``extensions``, as TeX and bibtex add it."""
    names = [name] if name.lower().endswith(extensions) else [name + extensions[0], name]
    for folder in dict.fromkeys(folders):
        for candidate in names:
            path = _join(folder, candidate)
            if path is not None and path in files:
                return path
    return None


@dataclass
class _Document:
    root: str
    # Every .tex file it reads, itself first.
    files: list[str]
    # (bib path, tex path, line)
    bibs: list[tuple[str, str, int]]


def _read(root: str, loads: dict[str, list[latex.Load]], tex: set[str], bib: set[str]) -> _Document:
    """The document ``root`` with every file it includes, and the .bib files
    those load. Relative names are looked for where TeX finds them, the
    root's folder or the folder of ``\\import``, and else in the including
    file's own folder, which pdflatex does not do but authors expect."""
    top = posixpath.dirname(root)
    seen = {root}
    order = [root]
    found: list[tuple[str, str, int]] = []
    queue = [(root, top)]
    while queue:
        path, here = queue.pop(0)
        own = posixpath.dirname(path)
        for load in loads.get(path, []):
            if load.kind == "bib":
                hit = _find(load.name, [top, here, own, ""], bib, (".bib",))
                if hit is not None:
                    found.append((hit, path, load.line))
                continue
            if load.folder:
                folder = _join(here if load.nested else top, load.folder)
                if folder is None:
                    continue
                target = _find(load.name, [folder], tex, TEX_EXTENSIONS)
                nxt = folder
            else:
                target = _find(load.name, [here, own], tex, TEX_EXTENSIONS)
                nxt = here
            if target is not None and target not in seen:
                seen.add(target)
                order.append(target)
                queue.append((target, nxt))
    return _Document(root, order, found)


def scope(texts: dict[str, str], main_file: str, mode: str, chosen: list[str] | None) -> Scope:
    """The scope of a project's check. ``texts`` holds every .tex and .bib
    text of the project by path."""
    tex = sorted(p for p in texts if is_tex(p))
    bib_paths = sorted(p for p in texts if is_bib(p))
    tex_set, bib_set = set(tex), set(bib_paths)
    # Both in one pass, so each text is prepared once (latex keeps a few).
    loads: dict[str, list[latex.Load]] = {}
    roots: list[str] = []
    for p in tex:
        loads[p] = latex.loads(texts[p])
        if latex.is_document(texts[p]):
            roots.append(p)
    if main_file in tex_set and main_file not in roots:
        roots.insert(0, main_file)
    documents = [_read(r, loads, tex_set, bib_set) for r in roots]
    # A .tex file no document includes is read as one of its own.
    reached = {p for d in documents for p in d.files}
    for p in tex:
        if p not in reached:
            d = _read(p, loads, tex_set, bib_set)
            documents.append(d)
            reached.update(d.files)

    bibs = {p: Bib(p) for p in bib_paths}
    for d in documents:
        for bib_path, tex_path, line in d.bibs:
            where = (tex_path, line)
            if where not in bibs[bib_path].loaded_by:
                bibs[bib_path].loaded_by.append(where)
            if d.root == main_file:
                bibs[bib_path].main = True
    for b in bibs.values():
        b.loaded_by.sort()

    fallback = False
    if mode == "all":
        checked = list(bib_paths)
    elif mode == "chosen":
        wanted = set(chosen or [])
        checked = [p for p in bib_paths if p in wanted]
    else:
        mode = "main"
        checked = [p for p in bib_paths if bibs[p].main]
        if not checked:
            checked = list(bib_paths)
            fallback = True
    for p in checked:
        bibs[p].checked = True

    if mode == "all" or fallback:
        counted = list(tex)
    else:
        keep = set(checked)
        counted_set: set[str] = set()
        for d in documents:
            read = {b for b, _t, _l in d.bibs}
            if not read or read & keep:
                counted_set.update(d.files)
        counted = sorted(counted_set)
    return Scope(mode, [bibs[p] for p in bib_paths], checked, counted, fallback)
