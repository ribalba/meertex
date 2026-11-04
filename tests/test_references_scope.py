"""Which .bib files a check reads and which .tex files count (docs/DESIGN.md
§16, Files): the commands that load files, how their names are found and the
three modes. No database, no network."""

from app.references import latex
from app.references.scope import scope


def _loads(text: str) -> list[tuple[str, str, int, str, str, bool]]:
    return [(x.kind, x.name, x.line, x.command, x.folder, x.nested) for x in latex.loads(text)]


def test_loads_finds_bibliographies_and_inputs():
    text = r"""\documentclass{article}
\addbibresource[location=local]{refs.bib}
\begin{document}
\input{sections/intro}
\include{appendix}
\input body.tex
\subfile{parts/one}
\import{chapters/}{two}
\subimport*{deep}{three.tex}
\bibliography{refs, extra}
\nobibliography{other}
\end{document}
"""
    assert _loads(text) == [
        ("bib", "refs.bib", 2, "addbibresource", "", False),
        ("tex", "sections/intro", 4, "input", "", False),
        ("tex", "appendix", 5, "include", "", False),
        ("tex", "body.tex", 6, "input", "", False),
        ("tex", "parts/one", 7, "subfile", "", False),
        ("tex", "two", 8, "import", "chapters/", False),
        ("tex", "three.tex", 9, "subimport", "deep", True),
        ("bib", "refs", 10, "bibliography", "", False),
        ("bib", "extra", 10, "bibliography", "", False),
        ("bib", "other", 11, "nobibliography", "", False),
    ]
    assert latex.is_document(text)


def test_loads_skips_comments_verbatim_and_macros():
    text = r"""% \bibliography{commented}
\verb|\input{verbatim}|
\begin{verbatim}
\bibliography{inside}
\end{verbatim}
\newcommand{\chapter}[1]{\input{#1}}
\bibliography{\jobname}
\bibliographystyle{plain}
\inputencoding{utf8}
50\% \input{kept}
"""
    assert [(x.kind, x.name) for x in latex.loads(text)] == [("tex", "kept")]
    assert not latex.is_document(r"% \documentclass{article}")


MAIN = "\\documentclass{article}\n\\begin{document}\n\\input{sections/refs}\n\\end{document}\n"


def test_names_are_found_like_tex_and_bibtex_find_them():
    texts = {
        "main.tex": MAIN,
        # Included by main.tex, so its name is relative to main.tex's folder.
        "sections/refs.tex": "\\bibliography{bib/refs,missing}\n",
        "bib/refs.bib": "",
        "sections/bib/refs.bib": "",
        # A paper of its own in a folder, with a copy.
        "submitted/paper.tex": "\\documentclass{article}\n\\addbibresource{refs.bib}\n",
        "submitted/refs.bib": "",
        "notes.bib": "",
    }
    s = scope(texts, "main.tex", "main", [])
    bibs = {b.path: b for b in s.bibs}
    assert bibs["bib/refs.bib"].loaded_by == [("sections/refs.tex", 1)]
    assert bibs["bib/refs.bib"].main
    assert bibs["sections/bib/refs.bib"].loaded_by == []
    assert bibs["submitted/refs.bib"].loaded_by == [("submitted/paper.tex", 2)]
    assert not bibs["submitted/refs.bib"].main
    assert bibs["notes.bib"].loaded_by == []
    assert s.checked == ["bib/refs.bib"]
    assert s.tex == ["main.tex", "sections/refs.tex"]
    assert not s.fallback


def test_import_folders_and_a_files_own_folder():
    texts = {
        "paper/main.tex": "\\documentclass{article}\n\\import{chapters}{one}\n\\input{local}\n",
        # \import makes its folder the base of the files it reads.
        "paper/chapters/one.tex": "\\subimport{deeper}{two}\n",
        "paper/chapters/deeper/two.tex": "\\bibliography{../../refs}\n",
        "paper/local.tex": "",
        "paper/refs.bib": "",
        "elsewhere.tex": "\\bibliography{paper/refs}\n",
    }
    s = scope(texts, "paper/main.tex", "main", [])
    (bib,) = s.bibs
    # bibtex runs in the main file's folder. The file's own folder is tried
    # after it, and that is how ../../refs is found. elsewhere.tex is read as
    # a document of its own.
    assert bib.loaded_by == [("elsewhere.tex", 1), ("paper/chapters/deeper/two.tex", 1)]
    assert bib.main
    assert s.tex == ["elsewhere.tex", "paper/chapters/deeper/two.tex", "paper/chapters/one.tex", "paper/local.tex",
                     "paper/main.tex"]


def test_modes():
    texts = {
        "main.tex": "\\documentclass{article}\n\\bibliography{refs}\n\\cite{a}\n",
        "refs.bib": "",
        "submitted/main.tex": "\\documentclass{article}\n\\bibliography{refs}\n",
        "submitted/refs.bib": "",
        "letter.tex": "\\documentclass{letter}\n\\cite{a}\n",
    }
    s = scope(texts, "main.tex", "main", [])
    assert s.checked == ["refs.bib"]
    # The letter loads no .bib file, so its \cite keys still count.
    assert s.tex == ["letter.tex", "main.tex"]
    s = scope(texts, "main.tex", "all", [])
    assert s.checked == ["refs.bib", "submitted/refs.bib"]
    assert s.tex == ["letter.tex", "main.tex", "submitted/main.tex"]
    s = scope(texts, "main.tex", "chosen", ["submitted/refs.bib", "gone.bib"])
    assert s.checked == ["submitted/refs.bib"]
    assert s.tex == ["letter.tex", "submitted/main.tex"]
    assert [b.checked for b in s.bibs] == [False, True]
    # A mode it does not know is the default.
    assert scope(texts, "main.tex", "bogus", []).mode == "main"


def test_a_main_file_that_loads_nothing_reads_everything():
    texts = {"main.tex": "\\documentclass{article}\n", "a.bib": "", "b.bib": "", "other.tex": "\\bibliography{a}\n"}
    s = scope(texts, "main.tex", "main", [])
    assert s.fallback
    assert s.checked == ["a.bib", "b.bib"]
    assert s.tex == ["main.tex", "other.tex"]
    # Also when the main file is not there at all.
    assert scope(texts, "paper.tex", "main", []).checked == ["a.bib", "b.bib"]


def test_names_outside_the_project_are_not_found():
    texts = {"main.tex": "\\bibliography{../refs}\n\\addbibresource{/etc/refs.bib}\n", "refs.bib": ""}
    (bib,) = scope(texts, "main.tex", "main", []).bibs
    assert bib.loaded_by == []
