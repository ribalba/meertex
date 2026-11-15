"""A Markdown main file in the compile service (compile/service.py and
compile/markdown.lua, docs/DESIGN.md §14 Markdown): pandoc turns it into
LaTeX, and SyncTeX and TeX's errors speak of the Markdown's lines."""

import hashlib
import importlib.util
import re
import struct
import uuid
import zlib

import httpx
import pytest
from compile_helpers import (
    SERVICE,
    TOKEN,
    compile_service,
    needs_pandoc,
    needs_tex,
    tgz,
)

AUTH = {"Authorization": f"Bearer {TOKEN}"}

_spec = importlib.util.spec_from_file_location("meertex_compile_service", SERVICE)
service_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(service_module)

NOTES = b"""---
title: Notes
---

# Introduction

The first paragraph has *emphasis*,
a second line with 50% of the `code`,
and a third line with a footnote[^1].

- An item
- Another item
  that goes on

| A | B |
|---|---|
| 1 | 2 |

- [ ] a task
- [x] a done task

Math $E = mc^2$ and a hard break\\
after it.

[^1]: The footnote.
"""


# --- The line map, without TeX -------------------------------------------------------


def test_the_lines_of_the_latex():
    tex = r"""\documentclass{article}
% a comment of the template %meertex:99
\begin{document}
\maketitle

%meertex:5

\section{Introduction}

%meertex:7
First line, %meertex:8
second line\footnote{%meertex:24
  The footnote.} and 50\%meertex:9
on. %meertex:9
third line.
\end{document}"""
    lines = service_module.markdown_lines(tex)
    assert lines == [0, 0, 0, 1, 0, 0, 0, 5, 0, 0, 7, 8, 24, 24, 9, 9]
    assert service_module.markdown_line(lines, 11) == 7
    assert service_module.markdown_line(lines, 13) == 24
    assert service_module.markdown_line(lines, 15) == 9
    # A line without text takes the one before it.
    assert service_module.markdown_line(lines, 9) == 5
    assert service_module.markdown_line(lines, 1) == 1
    assert service_module.markdown_line(lines, 999) == 9

    assert service_module.latex_line(lines, 8) == 12
    assert service_module.latex_line(lines, 24) == 13
    # An empty line of the Markdown shows where the line before it is.
    assert service_module.latex_line(lines, 6) == 8
    # The front matter is where the title is.
    assert service_module.latex_line(lines, 2) == 4
    assert service_module.latex_line(lines, 0) == 4


def test_errors_are_moved_to_the_markdown():
    lines = [0, 0, 7, 7, 12]
    log = (
        "(./notes.md.tex\n"
        "./notes.md.tex:5: Undefined control sequence.\n"
        "./other.tex:5: Undefined control sequence.\n"
        "LaTeX Warning: Reference `x' on page 1 undefined on input line 4.\n"
    )
    mapped = service_module.map_markdown_log(log, "docs/notes.md", lines)
    assert "./notes.md:12: Undefined control sequence." in mapped
    assert "./other.tex:5:" in mapped
    assert "(./notes.md.tex" in mapped and "on input line 4." in mapped
    assert service_module.tex_name("docs/notes.md") == "docs/notes.md.tex"
    assert service_module.tex_name("Notes.MARKDOWN") == "Notes.MARKDOWN.tex"
    assert service_module.tex_name("main.tex") == "main.tex"


# --- Compiling ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    with compile_service(tmp_path_factory.mktemp("markdown")) as svc:
        yield svc


def _ok(svc, files: dict[str, bytes], main: str = "notes.md", compiler: str = "pdflatex", path: str = "/jobs") -> dict:
    r = httpx.post(
        svc.url + path,
        params={"main_file": main, "compiler": compiler},
        content=tgz(files),
        headers=AUTH,
        timeout=60,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _png() -> bytes:
    """One red pixel."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def _view(svc, job_id: str, file: str, line: int) -> list[dict]:
    r = httpx.post(f"{svc.url}/jobs/{job_id}/synctex/view", json={"file": file, "line": line}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def _edit(svc, job_id: str, box: dict) -> dict:
    point = {"page": box["page"], "x": box["x"] + 3, "y": box["y"] + box["height"] / 2}
    r = httpx.post(f"{svc.url}/jobs/{job_id}/synctex/edit", json=point, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


@needs_tex
@needs_pandoc
def test_markdown_compiles_and_synctex_speaks_of_its_lines(service):
    job = _ok(service, {"notes.md": NOTES})
    assert job["status"] == "success", job["log"][-3000:]
    pdf = httpx.get(f"{service.url}/jobs/{job['job_id']}/output.pdf", headers=AUTH)
    assert pdf.content.startswith(b"%PDF-")

    tex = (service.jobs / job["job_id"] / "tree" / "notes.md.tex").read_text()
    assert "%meertex:8" in tex
    # The text's own % is the LaTeX's \%, and no mark.
    assert "50\\%" in tex
    # sourcepos leaves no braces around every word behind.
    assert "{first}" not in tex and "wrapper" not in tex
    # The task list's boxes are symbols pdfLaTeX can set.
    assert "$\\square$" in tex and "$\\boxtimes$" in tex

    # The heading, a paragraph, list items and tasks, the line after a hard
    # break and the footnote start lines of the PDF.
    for line in (5, 7, 11, 12, 19, 20, 23, 25):
        boxes = _view(service, job["job_id"], "notes.md", line)
        assert boxes, line
        assert _edit(service, job["job_id"], boxes[0]) == {"file": "notes.md", "line": line, "column": 0}
    assert _view(service, job["job_id"], "notes.md", 7)[0]["y"] > _view(service, job["job_id"], "notes.md", 5)[0]["y"]

    # The PDF puts the Markdown's lines of a paragraph side by side. SyncTeX
    # knows each word's line, from left to right.
    def across(line: int) -> list[int]:
        box = _view(service, job["job_id"], "notes.md", line)[0]
        seen = []
        for x in range(int(box["x"]) + 2, int(box["x"] + box["width"]), 12):
            r = httpx.post(
                f"{service.url}/jobs/{job['job_id']}/synctex/edit",
                json={"page": box["page"], "x": x, "y": box["y"] + box["height"] / 2},
                headers=AUTH,
            )
            if r.status_code == 200:
                seen.append(r.json()["line"])
        return list(dict.fromkeys(seen))

    assert [n for n in across(8) if n in (7, 8, 9)] == [7, 8, 9]
    assert [n for n in across(13) if n in (12, 13)] == [12, 13]


@needs_tex
@needs_pandoc
@pytest.mark.parametrize("compiler", ["xelatex", "lualatex"])
def test_markdown_with_other_compilers(service, compiler):
    job = _ok(service, {"notes.md": NOTES + "Grüße → Ünïcödé.\n".encode()}, compiler=compiler)
    assert job["status"] == "success", job["log"][-3000:]


@needs_tex
@needs_pandoc
def test_markdown_in_a_folder_with_a_figure_and_raw_latex(service):
    doc = b"# Results\n\n![A red dot](fig/dot.png)\n\nSee `\\textbf{this}`{=latex}.\n"
    job = _ok(service, {"docs/report.md": doc, "docs/fig/dot.png": _png()}, main="docs/report.md")
    assert job["status"] == "success", job["log"][-3000:]
    tex = (service.jobs / job["job_id"] / "tree" / "docs" / "report.md.tex").read_text()
    assert "\\begin{figure}" in tex and "\\caption{A red dot}" in tex
    assert "\\textbf{this}" in tex
    boxes = _view(service, job["job_id"], "docs/report.md", 5)
    assert _edit(service, job["job_id"], boxes[0]) == {"file": "docs/report.md", "line": 5, "column": 0}


@needs_tex
@needs_pandoc
def test_errors_name_the_markdown_line(service):
    broken = NOTES.replace(b"after it.", b"after `\\undefinedmacro`{=latex} it.")
    job = _ok(service, {"notes.md": broken})
    assert job["status"] == "errors"
    assert "./notes.md:23: Undefined control sequence." in job["log"]
    assert re.search(r"notes\.md\.tex:\d+:", job["log"]) is None


@needs_tex
@needs_pandoc
def test_a_paper_file_with_the_latex_name_does_not_win(service):
    job = _ok(service, {"notes.md": NOTES, "notes.md.tex": b"\\documentclass{article}\\begin{document}Old\\end{document}\n"})
    assert job["status"] == "success", job["log"][-3000:]
    assert "%meertex:" in (service.jobs / job["job_id"] / "tree" / "notes.md.tex").read_text()


def _in_workspace(svc, key: str, files: dict[str, bytes], main: str = "notes.md"):
    manifest = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
    r = httpx.post(f"{svc.url}/workspaces/{key}/prepare", json={"files": manifest}, headers=AUTH, timeout=60)
    assert r.status_code == 200, r.text
    plan = r.json()
    params = {"main_file": main, "compiler": "pdflatex", "workspace": key, "token": plan["token"]}
    r = httpx.post(svc.url + "/jobs", params=params, content=tgz({p: files[p] for p in plan["need"]}), headers=AUTH, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


@needs_tex
@needs_pandoc
def test_markdown_in_a_workspace(service):
    key = str(uuid.uuid4())
    files = {"notes.md": NOTES}
    first = _in_workspace(service, key, files)
    assert first["status"] == "success", first["log"][-3000:]
    files["notes.md"] = NOTES.replace(b"An item", b"A changed item")
    second = _in_workspace(service, key, files)
    assert second["status"] == "success", second["log"][-3000:]
    workspace = service.jobs / "workspaces" / key
    # pandoc makes the LaTeX for every compile, so it is no build file to keep.
    assert not (workspace / "seeds" / "notes.md.tex").exists()
    assert (workspace / "seeds" / "notes.md.aux").exists()

    # Both jobs answer for their own PDF, also after the next preparation
    # swept the LaTeX away.
    manifest = {"notes.md": hashlib.sha256(files["notes.md"]).hexdigest()}
    httpx.post(f"{service.url}/workspaces/{key}/prepare", json={"files": manifest}, headers=AUTH, timeout=60)
    assert not (workspace / "tree" / "notes.md.tex").exists()
    for job in (first, second):
        boxes = _view(service, job["job_id"], "notes.md", 12)
        assert boxes
        assert _edit(service, job["job_id"], boxes[0]) == {"file": "notes.md", "line": 12, "column": 0}


@needs_tex
@needs_pandoc
def test_latexdiff_of_markdown(service):
    new = NOTES.replace(b"The first paragraph", b"The changed first paragraph")
    job = _ok(service, {"old/notes.md": NOTES, "new/notes.md": new}, path="/latexdiff")
    assert job["status"] == "success", job["log"][-3000:]
    diff = (service.jobs / job["job_id"] / "tree" / "notes.md.tex").read_text()
    assert "\\DIFadd" in diff
    assert "%meertex:" not in diff

    job = _ok(service, {"old/other.md": NOTES, "new/notes.md": new}, path="/latexdiff")
    assert job["status"] == "failure"
    assert "not in the older version" in job["log"]
