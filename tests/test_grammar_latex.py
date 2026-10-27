"""Paragraphs and LaTeX as LanguageTool's annotated text (DESIGN §17).

The one hard rule is pinned on every case and on random input. The items,
joined, give back the paragraph exactly. Then every offset LanguageTool
reports is an offset into the source, and the editor's underlines land on the
right characters. The rest pins what LanguageTool gets to read, which is what
decides whether a correct paper stays quiet.
"""

import random

import pytest

from app.grammar import checker
from app.grammar.latex import annotate, annotation, has_text, interpreted, paragraphs


def read(source: str) -> str:
    """What LanguageTool reads for ``source``, after checking the invariant."""
    items = annotate(source)
    assert "".join(src for src, _ in items) == source
    return interpreted(items)


# --- What LanguageTool reads ---------------------------------------------------------


@pytest.mark.parametrize(
    "latex, text",
    [
        # Plain prose is passed through as it is.
        ("Plain prose, nothing else.", "Plain prose, nothing else."),
        ("", ""),
        # Comments are markup, their newline is text.
        ("Before % a comment\nafter.", "Before after."),
        ("Before% a comment\nafter.", "Before\nafter."),
        ("A \\% sign and 100\\% sure.", "A % sign and 100% sure."),
        ("% only a comment", ""),
        # Inline math is a dummy word, Ina<n> where "an" belongs.
        ("We use $x$ here.", "We use Ina1 here."),
        ("We use $k$ here.", "We use Dummy1 here."),
        ("an $n$-gram and a $k$-mer", "an Ina1-gram and a Dummy2-mer"),
        ("an $\\mathcal{O}(n)$ bound", "an Ina1 bound"),
        ("an $\\ell_2$ norm and a $\\beta$", "an Ina1 norm and a Dummy2"),
        ("an $8$-core and an $11$-fold and a $10$-fold", "an Ina1-core and an Ina2-fold and a Dummy3-fold"),
        ("Let $x$ $y$ be.", "Let Ina1 Dummy2 be."),
        ("the \\(i\\)th element", "the Ina1 element"),
        ("the $i$th and the $n$-th", "the Ina1 and the Ina2-th"),
        ("costs \\$5 and $x$", "costs $5 and Ina1"),
        # Display math is one item, and keeps a full stop or comma at its end.
        ("We have \\[ x = 1. \\] Then", "We have Ina1. Then"),
        ("We have $$ y = 2, $$ where", "We have Dummy1, where"),
        (
            "is\n\\begin{equation}\n  E = mc^2,\n  \\label{eq:e}\n\\end{equation}\nwhere $E$ is",
            "is\nIna1,\nwhere Ina2 is",
        ),
        ("so\n\\begin{align*}\n  a &= b \\\\\n  c &= d.\n\\end{align*}\nThen", "so\nIna1.\nThen"),
        ("in \\begin{math}k\\end{math} steps", "in Dummy1 steps"),
        # Citations, references, URLs and code are dummies with their arguments.
        ("shown~\\cite{a,b}.", "shown Dummy1."),
        ("as \\citet[p.~5]{a} said", "as Dummy1 said"),
        ("see \\citep[see][]{x}.", "see Dummy1."),
        ("Section~\\ref{s} and \\eqref{e}", "Section Dummy1 and Dummy2"),
        ("\\Cref{a} and \\autoref{b}", "Dummy1 and Dummy2"),
        ("text\\cite{x} more", "text Dummy1 more"),
        ("at \\url{http://a.b/%7e} now", "at Dummy1 now"),
        ("run \\texttt{make all} first", "run Dummy1 first"),
        # Code takes "a" or "an" by its first letter where words and letters
        # agree, and otherwise by the article before it.
        ("an \\texttt{ollama}, an \\texttt{energy\\_model}, an \\texttt{[IDLE]}", "an Ina1, an Ina2, an Ina3"),
        ("a \\texttt{daemon}, a \\texttt{file}, an \\texttt{fd}, a \\texttt{8x}", "a Dummy1, a Dummy2, an Ina3, a Ina4"),
        ("an \\url{https://x.org} and a \\url{https://y.org}", "an Ina1 and a Dummy2"),
        ("an \\verb|ls| and a \\verb|cat|", "an Ina1 and a Dummy2"),
        # A citation or a reference says nothing about its sound, so it takes
        # the article before it.
        ("an~\\ref{x}, a \\ref{y}, \\cite{z}, An \\cite{w}", "an Ina1, a Dummy2, Dummy3, An Ina4"),
        ("Kaplan \\ref{x} and Ian \\cite{y}", "Kaplan Dummy1 and Ian Dummy2"),
        ("an \\SI{8}{\\watt} and a \\SI{5}{\\joule} run", "an Ina1 and a Dummy2 run"),
        ("an \\ac{LLM} and a \\ac{GPU} and \\gls{x}", "an Ina1 and a Dummy2 and Ina3"),
        ("run \\verb|x%y| and \\verb*+z+ then", "run Dummy1 and Dummy2 then"),
        ("our \\href{http://x.org/a%20b}{web site} has", "our web site has"),
        # Commands that print nothing, with their arguments.
        ("Text\\label{x} more.", "Text more."),
        ("Text \\label{x} more.", "Text more."),
        ("A \\vspace{1em} B", "A B"),
        ("said \\foo{x}.", "said."),
        ("said \\foo{x} today.", "said today."),
        ("\\noindent The start.", "The start."),
        ("\\input{chapter} done", "done"),
        ("We \\todo[inline]{fix this} go.", "We go."),
        # A command without arguments on a line with prose is the paper's own
        # macro, usually a number from a generated file, and reads as one.
        ("a factor of\n\\RepSessSDRatioMedian, because", "a factor of\n101, because"),
        ("costs (\\OvhCheckJLo{} to \\OvhCheckJHi) with", "costs (101 to 102) with"),
        ("\\CorpusScriptedApps{} applications were recorded.", "101 applications were recorded."),
        ("of \\WebBlockParrotOk{} of them.", "of 101 of them."),
        ("costs \\OvhCheckJ\\,J (\\DriftPct\\,\\%).", "costs 101 J (102 %)."),
        ("We use \\sys, which is an \\Tool{} and a \\Thing.", "We use 101, which is an 802 and a 103."),
        ("\\WebHeadlessGood.", "101."),
        ("glued\\Macro{} and $x$\\Other", "glued 101 and Ina2 103"),
        # Alone on its line it prints nothing, nor do layout and font commands.
        ("Text.\n\\sectionbreak\nMore.", "Text.\nMore."),
        ("Text.\n  \\CorpusApps{} % a comment\nMore.", "Text.\nMore."),
        ("\\noindent\\small Some \\hfill text\\xspace{} and {\\em more}\\par", "Some text and more"),
        ("the \\Large first and \\bfseries second", "the first and second"),
        # Prose commands keep their text.
        ("This is \\emph{very} important.", "This is very important."),
        ("\\textbf{\\emph{Nested}} words", "Nested words"),
        ("\\textcolor{red}{Red} text", "Red text"),
        ("\\textcolor[rgb]{1,0,0}{Red} text", "Red text"),
        ("a \\mbox{whole} word", "a whole word"),
        # Headings and captions are paragraphs of their own.
        ("\\section{Introduction}\\label{sec:intro}", "Introduction\n\n"),
        ("\\section*{Related Work}\nPrior systems", "Related Work\n\nPrior systems"),
        ("\\subsection[Short]{A Long Title}", "A Long Title\n\n"),
        ("\\paragraph{Setup.} We ran", "Setup.\n\nWe ran"),
        ("\\caption{Results of the run.}", "Results of the run.\n\n"),
        # A footnote reads as a parenthesis.
        ("This holds.\\footnote{See X.} Next.", "This holds. (See X.) Next."),
        ("A claim\\footnote{See X.} that holds.", "A claim (See X.) that holds."),
        ("A claim \\footnote{See X.} that holds.", "A claim (See X.) that holds."),
        # Whitespace at the edges of a footnote does not end up inside its
        # parenthesis.
        ("(Alibaba).\\footnote{\\raggedright Neuralwatt model IDs.}", "(Alibaba). (Neuralwatt model IDs.)"),
        ("A claim\\footnote{ See X. } that.", "A claim (See X.) that."),
        ("A claim\\footnote{See X.\n\\label{fn}} that.", "A claim (See X.) that."),
        # In lists \item is a bullet.
        (
            "We have:\n\\begin{itemize}\n  \\item a method;\n  \\item[b)] a dataset.\n\\end{itemize}",
            "We have:\n\u2022 a method;\n\u2022 a dataset.\n",
        ),
        # Other environments keep their content, \begin and \end print nothing.
        ("\\begin{abstract}\nWe show.\n\\end{abstract}", "We show.\n"),
        ("\\begin{minipage}[t]{0.5\\linewidth}\nText.\n\\end{minipage}", "Text.\n"),
        ("\\begin{theorem}[Pythagoras] It holds.\\end{theorem}", "It holds."),
        # Skipped environments are markup as a whole.
        ("Before\n\\begin{tabular}{ll}\na & b \\\\\n\\end{tabular}\nafter", "Before\nafter"),
        ("Before\n\\begin{verbatim}\n% not a comment } {\n\\end{verbatim}\nafter", "Before\nafter"),
        ("A\n\\begin{lstlisting}[language=C]\nint x;\n\\end{lstlisting}\nB", "A\nB"),
        ("A\n\\begin{tabular}{l}\\begin{tabular}{l}x\\end{tabular}\\end{tabular}\nB", "A\nB"),
        ("A \\begin{tikzpicture}\\draw (0,0);\\end{tikzpicture} B", "A B"),
        # Keyword lists are no sentences, and the ACM classification is XML.
        ("\\begin{IEEEkeywords}\ngreen software, energy\n\\end{IEEEkeywords}", ""),
        ("\\begin{keywords} a, b \\end{keywords}", ""),
        ("\\begin{CCSXML}\n<ccs2012><concept_id>10011007</concept_id></ccs2012>\n\\end{CCSXML}", ""),
        # Spacing, characters and words.
        ("Figure~1 and a\\ b and x\\,y and p\\quad q", "Figure 1 and a b and x y and p q"),
        ("one\\\\two\\\\[2pt]three", "one two three"),
        ("one \\\\\ntwo", "one two"),
        ("\\& \\# \\_ \\{ \\}", "& # _ { }"),
        ("pages 10--20, a pause---so", "pages 10\u201320, a pause\u2014so"),
        ("``quoted'' and it's", "\u201cquoted\u201d and it's"),
        ("wait\\ldots{} and \\dots", "wait\u2026 and \u2026"),
        ("\\LaTeX{} and \\TeX", "LaTeX and TeX"),
        ("for example \\eg this, \\ie that, \\etc.", "for example e.g. this, i.e. that, etc."),
        ("hy\\-phen\\-ation", "hyphenation"),
        # Accents compose.
        ('M\\"{u}ller and G\\"odel', "M\u00fcller and G\u00f6del"),
        ("\\'e \\`e \\^o \\~n \\c{c} \\c c", "\u00e9 \u00e8 \u00f4 \u00f1 \u00e7 \u00e7"),
        ('na\\"\\i ve and \\\'{\\i}', "na\u00efve and \u00ed"),
        ("stra\\ss e and S\\o ren", "stra\u00dfe and S\u00f8ren"),
        ("{\\v S}koda", "\u0160koda"),
        # Broken input never raises, and becomes markup.
        ("Unbalanced { brace", "Unbalanced brace"),
        ("stray } brace", "stray brace"),
        ("open $math to the end", "open Ina1"),
        ("open \\[ display", "open Dummy1"),
        ("\\begin{equation} never closed", "Ina1"),
        ("\\emph{never closed", "never closed"),
        ("\\cite{never closed", "Dummy1"),
        ("trailing \\", "trailing "),
        ("\\begin{", ""),
        ("\\verb", ""),
    ],
)
def test_what_languagetool_reads(latex, text):
    assert read(latex) == text


def test_indentation_and_whitespace_left_by_markup_are_markup():
    """LanguageTool reports two spaces in a row and spaces after a line break,
    so neither may come from the way LaTeX is written."""
    source = "  Indented start.\n    Indented line \\label{a} with \\foo{x} label.\n\t\\vspace{1em}\n\tEnd."
    assert read(source) == "Indented start.\nIndented line with label.\nEnd."


def test_a_text_item_never_holds_markup_and_vice_versa():
    items = annotate("A $x$ \\cite{y} and \\emph{z}.")
    assert ("$x$", "Ina1") in items
    assert all("\\" not in src for src, interp in items if interp is None)


@pytest.mark.parametrize("kind", ["text", "markdown"])
def test_plain_text_is_one_text_item(kind):
    source = "A $x$ is \\emph{not} LaTeX here."
    assert annotate(source, kind) == [(source, None)]


def test_has_text():
    assert has_text(annotate("Some words."))
    assert not has_text(annotate("\\label{x}\n\\begin{figure}\n\\includegraphics{a}\n\\end{figure}"))
    assert not has_text(annotate("$x = 1$"))
    assert not has_text(annotate("% just a comment"))
    assert not has_text(annotate("   "))


def test_the_annotation_json_shape():
    items = annotate("A \\label{x}$y$.")
    assert annotation(items) == [
        {"text": "A "},
        {"markup": "\\label{x}$y$", "interpretAs": "Dummy1"},
        {"text": "."},
    ]
    # Markup that stands for nothing goes without interpretAs.
    assert annotation([("%c", "")]) == [{"markup": "%c"}]


def test_offsets_count_utf16_units_with_a_non_bmp_character():
    """𝔸 is one code point to Python and two units to Java and JavaScript.
    The text spans the checker drops markup-only matches with are counted the
    way LanguageTool counts, so "Teh" after it is at unit 10, not 9."""
    source = "𝔸 is $x$ Teh"
    assert source.index("Teh") == 9
    items = annotate(source)
    assert checker._text_spans(items) == [(0, 6), (9, 13)]
    assert checker._slice(source, 10, 3) == "Teh"


# --- Random input -------------------------------------------------------------------

_PIECES = [
    "\\", "{", "}", "[", "]", "$", "$$", "%", "~", "-", "--", "`", "'", "''", "``", "&", "#", "_", "^",
    "\\\\", "\\[", "\\]", "\\(", "\\)", "\\%", "\\$", "\\{", "\\}", "\\ ", "\\,", '\\"', "\\'", "\\c", "\\i",
    "\\emph{", "\\cite{", "\\ref", "\\section*{", "\\footnote{", "\\href{", "\\url{", "\\verb|", "\\item",
    "\\begin{", "\\end{", "itemize}", "equation}", "verbatim}", "tabular}", "document}", "\\label{",
    "\\textcolor{red}{", "\\foo", "\\ss", "\\LaTeX", "word", "Teh", " ", "  ", "\n", "\n\n", "\t", "a", "x",
    ".", ",", "𝔸", "ü", "\ud800",
]


@pytest.mark.parametrize("seed", range(300))
def test_random_latex_keeps_the_invariant(seed):
    rng = random.Random(seed)
    source = "".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 120)))
    items = annotate(source)
    assert "".join(src for src, _ in items) == source
    assert all(src for src, _ in items)
    annotation(items)
    # And the paragraphs are in order, inside the text, and never overlap.
    last = 0
    for start, end in paragraphs(source):
        assert last <= start <= end <= len(source)
        last = end
        piece = source[start:end]
        assert "".join(src for src, _ in annotate(piece)) == piece


def test_deep_nesting_does_not_run_out_of_stack():
    source = "{" * 5000 + "word" + "}" * 5000 + " \\emph{" * 3000 + "x"
    items = annotate(source)
    assert "".join(src for src, _ in items) == source


# --- Paragraphs ---------------------------------------------------------------------


def blocks(text: str, kind: str = "latex") -> list[str]:
    return [text[a:b] for a, b in paragraphs(text, kind)]


def test_paragraphs_are_blocks_between_blank_lines():
    text = "One line.\nSame paragraph.\n\nSecond.\n   \t\nThird.\n\n\n"
    assert blocks(text) == ["One line.\nSame paragraph.", "Second.", "Third."]


def test_a_comment_line_is_not_blank():
    text = "First\n% a comment\nstill first\n\nSecond"
    assert blocks(text) == ["First\n% a comment\nstill first", "Second"]


def test_only_the_document_body_is_checked():
    text = (
        "\\documentclass{article}\n\\usepackage{x}\n\\title{A Title}\n\n"
        "\\begin{document}\nFirst.\n\nSecond.\n\\end{document}\nAfter the end.\n\nMore."
    )
    assert blocks(text) == ["First.", "Second."]


def test_a_begin_document_in_a_comment_does_not_count():
    text = "% \\begin{document}\nFirst.\n\nSecond."
    assert blocks(text) == ["% \\begin{document}\nFirst.", "Second."]


def test_a_file_without_begin_document_is_checked_whole():
    text = "\\chapter{One}\nText.\n\n\\end{document}"
    assert blocks(text) == ["\\chapter{One}\nText.", "\\end{document}"]


def test_text_after_begin_document_on_its_line_counts():
    assert blocks("\\begin{document}Hello.\n\nNext.") == ["Hello.", "Next."]


def test_blank_lines_inside_a_skipped_environment_do_not_split():
    text = (
        "Before.\n\n\\begin{verbatim}\nline\n\n% \\end{tabular}\n\nmore\n\\end{verbatim}\n\nAfter.\n\n"
        "\\begin{tabular}{l}\na \\\\\n\nb\n\\end{tabular}\n\n\\begin{equation}\nx\n\n\\end{equation}"
    )
    assert blocks(text) == [
        "Before.",
        "\\begin{verbatim}\nline\n\n% \\end{tabular}\n\nmore\n\\end{verbatim}",
        "After.",
        "\\begin{tabular}{l}\na \\\\\n\nb\n\\end{tabular}",
        "\\begin{equation}\nx\n\n\\end{equation}",
    ]
    # And the verbatim block is all markup.
    assert not has_text(annotate(blocks(text)[1]))


def test_blank_lines_inside_the_acm_classification_do_not_split():
    text = "\\begin{CCSXML}\n<ccs2012>\n\n</ccs2012>\n\\end{CCSXML}\n\nText."
    assert blocks(text) == ["\\begin{CCSXML}\n<ccs2012>\n\n</ccs2012>\n\\end{CCSXML}", "Text."]


def test_an_environment_that_is_never_closed_does_not_swallow_the_file():
    """One that is being typed. The paragraphs after it stay paragraphs."""
    text = "Start \\begin{tabular}{l}\n\nNext paragraph.\n\nLast."
    assert blocks(text) == ["Start \\begin{tabular}{l}", "Next paragraph.", "Last."]


def test_plain_text_paragraphs_ignore_latex():
    text = "\\begin{document}\nA\n\n\\begin{verbatim}\nB\n\nC"
    assert blocks(text, "text") == ["\\begin{document}\nA", "\\begin{verbatim}\nB", "C"]


def test_crlf_line_ends():
    assert blocks("One.\r\nStill one.\r\n\r\nTwo.\r\n") == ["One.\r\nStill one.\r", "Two.\r"]
