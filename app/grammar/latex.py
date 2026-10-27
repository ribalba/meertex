"""Paragraphs, and LaTeX as LanguageTool's annotated text (docs/DESIGN.md §17).

LanguageTool can be sent "annotated text" instead of plain text, a list of
items, each one either text it checks or markup it skips, and a markup item
may say what it stands for (``interpretAs``). The offsets it answers with
count both kinds of item as they were sent. So when the items, joined, are
exactly the LaTeX source, every offset LanguageTool reports is already an
offset into the source and nothing has to be mapped back. That is the one
hard rule here, and the tests check it on every case and on random input:
``"".join(source of every item) == paragraph``.

The other rule is to stay quiet. A correct paper must get no underlines, so
whatever this module is not sure about becomes markup. Math and citations
become a made-up word (``Dummy1``), so the sentence around them still has a
word in that place and does not read as broken grammar. Formatting commands
keep their text and lose the command. Everything else prints nothing.

``paragraphs`` cuts a file into the blocks that are checked and cached one by
one, ``annotate`` turns one block into items, and ``annotation`` writes items
in the JSON shape LanguageTool reads.
"""

import re
import unicodedata

from ..references.latex import _ACCENTS, _DOTLESS, _LETTERS

# An item is (source, interpretAs). interpretAs None is text to check, a
# string (often "") is markup standing for that string.
Item = tuple[str, str | None]

# --- What the commands and environments are -------------------------------------

# Commands that stand for a word the reader sees and the checker cannot know:
# a citation, a reference number, a URL, code. They become a dummy word with
# all their arguments, so "see \ref{x} for" still has a word in the middle.
_DUMMY_COMMANDS = frozenset(
    {
        "cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "citeyearpar",
        "Cite", "Citep", "Citet", "Citeauthor",
        "parencite", "textcite", "autocite", "footcite", "Parencite", "Textcite", "Autocite",
        "ref", "eqref", "autoref", "cref", "Cref", "pageref", "nameref",
        "url", "nolinkurl", "path", "texttt",
    }
)
# Of those, the ones whose argument says how the dummy is said: "an
# \texttt{ollama}", "a \texttt{daemon}". The others (a citation, a reference)
# give no hint, so their dummy takes whichever article stands before it.
_CODE_COMMANDS = frozenset({"url", "nolinkurl", "path", "texttt"})
# Quantities (siunitx) and acronyms (acronym, glossaries) are dummies as well,
# but take "a" or "an" by their first argument as math does (an \SI{8}{\watt}
# machine, an \ac{LLM}).
_SPOKEN_COMMANDS = frozenset(
    {
        "SI", "si", "num", "qty", "unit", "SIrange", "qtyrange", "numrange", "ang",
        "ac", "acs", "acl", "acf", "acp", "acsp", "aclp", "acfp", "Ac", "Acs", "Acl", "Acf", "Acp",
        "gls", "Gls", "GLS", "glspl", "Glspl", "acrshort", "acrlong", "acrfull",
    }
)
# Their arguments are taken as written, without reading % as a comment. A URL
# may well have a %20 in it.
_RAW_ARGS = frozenset({"url", "nolinkurl", "path", "href"})

# Commands whose last argument is prose. The command, its braces and earlier
# arguments are markup, the argument itself is read like the rest of the text.
_PROSE = frozenset(
    {
        "emph", "textbf", "textit", "textsc", "textsf", "textrm", "textup", "textsl", "textmd",
        "textnormal", "underline", "mbox", "footnote", "enquote", "textcolor",
    }
)
# Headings, titles and captions are paragraphs of their own. Their closing brace
# stands for a blank line, so "Introduction" does not run into the sentence
# that follows it, and nothing asks for a full stop after a heading.
_HEADINGS = frozenset(
    {"title", "caption", "part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"}
)
# What the braces of a prose command stand for. A footnote reads as a
# parenthesis. Written mid-sentence, "claim\footnote{See X.} that" is then
# "claim (See X.) that", where a blank line would start a new paragraph with
# "that" and get it flagged for its small letter.
_BRACES = {
    "footnote": (" (", ")"),
    "enquote": ("\u201c", "\u201d"),
}

# Environments that are skipped as a whole: code, pictures, tables, the
# bibliography. Their content is not prose, or it is prose in cells, which
# reads as nonsense once the cells are run together.
_VERBATIM_ENVS = frozenset({"verbatim", "Verbatim", "lstlisting", "minted", "comment", "filecontents"})
_SKIPPED_ENVS = _VERBATIM_ENVS | frozenset(
    {
        "tikzpicture", "pgfpicture", "tabular", "tabular*", "tabularx", "longtable", "array",
        "algorithmic", "thebibliography",
        # Keyword lists are not sentences, and the ACM classification is XML.
        "IEEEkeywords", "keywords", "CCSXML",
    }
)
_MATH_ENVS = frozenset(
    {"equation", "align", "alignat", "gather", "multline", "flalign", "eqnarray", "displaymath", "math"}
)


def _starred(names: frozenset[str]) -> frozenset[str]:
    return names | frozenset(n + "*" for n in names if not n.endswith("*"))


_VERBATIM_ENVS = _starred(_VERBATIM_ENVS)
_SKIPPED_ENVS = _starred(_SKIPPED_ENVS)
_MATH_ENVS = _starred(_MATH_ENVS)
# Environments a blank line does not end, for the paragraph split. The whole
# environment is one markup item, so it has to be inside one paragraph.
_WHOLE_ENVS = _SKIPPED_ENVS | _MATH_ENVS

# Commands that print a fixed word or character. The last six are not LaTeX's
# own but are in most papers (cvpr.sty and many a preamble define them), and
# printing nothing for them would leave "This, , is" behind.
_WORDS = {
    "ldots": "\u2026",
    "dots": "\u2026",
    "textellipsis": "\u2026",
    "LaTeX": "LaTeX",
    "TeX": "TeX",
    "LaTeXe": "LaTeX2e",
    "BibTeX": "BibTeX",
    "textendash": "\u2013",
    "textemdash": "\u2014",
    "quad": " ",
    "qquad": " ",
    "eg": "e.g.",
    "ie": "i.e.",
    "etal": "et al.",
    "cf": "cf.",
    "etc": "etc.",
    "wrt": "w.r.t.",
}
# Commands without arguments that print nothing a reader would read: layout,
# spacing, page breaks, font switches. Any other command without arguments
# (or with only an empty {}) is a macro of the paper's own, usually a number
# from a generated file ("a factor of \RatioMedian,"), and becomes a dummy word
# when its line has prose on it. Alone on its line it prints nothing.
_INVISIBLE = frozenset(
    {
        "noindent", "indent", "centering", "raggedright", "raggedleft", "par", "newpage", "clearpage",
        "cleardoublepage", "pagebreak", "nopagebreak", "linebreak", "nolinebreak", "newline", "break",
        "allowbreak", "nobreak", "maketitle", "tableofcontents", "listoffigures", "listoftables",
        "appendix", "frontmatter", "mainmatter", "backmatter", "hline", "toprule", "midrule",
        "bottomrule", "hfill", "vfill", "hfil", "vfil", "smallskip", "medskip", "bigskip",
        "small", "footnotesize", "scriptsize", "tiny", "normalsize", "large", "Large", "LARGE", "huge",
        "Huge", "bfseries", "mdseries", "itshape", "slshape", "ttfamily", "sffamily", "rmfamily",
        "scshape", "upshape", "normalfont", "selectfont", "em", "bf", "it", "tt", "sf", "sc", "rm", "sl",
        "boldmath", "unboldmath", "protect", "relax", "xspace", "and", "balance", "sloppy", "fussy",
        "FloatBarrier", "onecolumn", "twocolumn", "IEEEpeerreviewmaketitle", "printbibliography",
        "null", "strut", "leavevmode", "unskip", "ignorespaces", "footnotemark", "newblock",
        "enspace", "thinspace", "endinput", "makeatletter", "makeatother", "iffalse", "fi", "else",
    }
)
# Accents written with a letter (\c{c}, \v s). The others are one symbol.
_LETTER_ACCENTS = frozenset(k for k in _ACCENTS if k.isalpha())
# Control symbols: spacing, escaped characters, and the ones that print nothing
# (\- a hyphenation point, \/ italic correction, \@ a spacing hint, \! a
# negative space).
_SYMBOLS = {
    " ": " ", "\n": " ", "\t": " ", ",": " ", ";": " ", ":": " ", ">": " ",
    "%": "%", "&": "&", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}",
    "-": "", "/": "", "@": "", "!": "",
}

# Math whose first letter is said with a vowel sound takes "an", as in an $x$
# (said "an ex"), an $n$ or an $8$. Its dummy is Ina<n>, which LanguageTool's
# a/an rule reads the same way, so "a $x$" is flagged and "an $x$" is not.
_VOWEL_SOUNDS = frozenset("aefhilmnorsx")
# Commands that only change how the next letter looks, so the letter is what
# is said, as in an $\mathcal{O}(n)$ bound or an $\hat{x}$.
_MATH_STYLES = frozenset(
    {
        "mathbf", "mathcal", "mathrm", "mathit", "mathsf", "mathtt", "mathbb", "mathfrak", "mathscr",
        "boldsymbol", "bm", "operatorname", "text", "textrm", "textit", "textbf", "mbox",
        "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
        "displaystyle", "textstyle", "hat", "widehat", "tilde", "widetilde", "bar", "overline",
        "vec", "dot", "ddot", "mathring",
    }
)
# Symbols whose names start with a vowel sound, as in an $\ell_2$ norm or an $\epsilon$.
_MATH_VOWELS = frozenset(
    {"alpha", "epsilon", "varepsilon", "eta", "iota", "omicron", "omega", "Omega", "upsilon", "Upsilon",
     "ell", "infty", "emptyset", "varnothing"}
)
_MATH_TOKEN = re.compile(r"\\([A-Za-z]+)|\\.|([A-Za-z])|([0-9]+)", re.DOTALL)
# Code and URLs are read as words or as letters, and the two agree only for
# some first letters. a, e, i and o are vowels either way (an ollama, an API),
# and b, c, d, g, j, k, p, q, t, v, w, y and z consonants either way. f, h, l,
# m, n, r, s, u and x can be both ("a file" but "an fd", "a user" but "an
# update").
_CODE_CONSONANTS = frozenset("bcdgjkpqtvwyz")
_COMMANDS_IN_CODE = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)
_CODE_WORD = re.compile(r"[A-Za-z0-9]+")
# "an" just before a dummy whose sound is unknown, so the dummy agrees with it.
_AN_BEFORE = re.compile(r"(?<![A-Za-z])[Aa]n[\s~]*\Z")
# What is not prose on a line: commands, braces, brackets and whitespace.
_NOT_PROSE = re.compile(r"\\(?:[A-Za-z@]+\*?|.)|[{}\[\]\s]", re.DOTALL)
_EMPTY_ARGS = re.compile(r"\s*(?:\{\s*\})?")

# Groups nested deeper than this are skipped as markup instead of read, so no
# input can run the reader out of stack.
_MAX_DEPTH = 64

_SPECIAL = re.compile(r"[\\%${}~`'\-]")
_BACKSLASH_OR_PERCENT = re.compile(r"[\\%]")
_LETTERS_RE = re.compile(r"[A-Za-z]+")
_ENV_NAME = re.compile(r"[A-Za-z@]+\*?")
# What may stand after the last symbol of display math before it closes.
_DISPLAY_TAIL = re.compile(r"(?:\\label\s*\{[^{}]*\}|\\nonumber|\\notag|\\\\)\s*\Z")
_DUMMY_WORD = re.compile(r"(?:Dummy|Ina)?\d")
_INDENT = re.compile(r"(?<=\n)[ \t\r]+")
_LEADING = re.compile(r"[ \t\r\n]+")
_CLOSING_PUNCTUATION = ".,;:!?)"
# What markup may stand for at its start that takes no space before it, and
# what may stand before text that takes no space after it.
_CLOSERS = ".,;:!?)\u201d"
_OPENERS = "(\u201c"
_SPACE = " \t\r\n"


# --- Small scanners ----------------------------------------------------------------


def _escaped(s: str, k: int) -> bool:
    """Whether the character at ``k`` follows an odd number of backslashes."""
    j = k
    while j > 0 and s[j - 1] == "\\":
        j -= 1
    return (k - j) % 2 == 1


def _find(s: str, token: str, start: int) -> int:
    """The next ``token`` from ``start`` that is not escaped, or -1."""
    while True:
        k = s.find(token, start)
        if k < 0 or not _escaped(s, k):
            return k
        start = k + 1


def _line_end(s: str, i: int) -> int:
    k = s.find("\n", i)
    return len(s) if k < 0 else k


def _group_end(s: str, i: int, raw: bool = False) -> int:
    """The position after the group whose ``{`` is at ``i``, or the end of
    ``s`` for one that is never closed. Escaped braces do not count, nor do
    braces in a % comment unless ``raw``."""
    depth, n, k = 0, len(s), i
    while k < n:
        c = s[k]
        if c == "\\" and not raw:
            k += 2
            continue
        if c == "%" and not raw:
            k = _line_end(s, k)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return n


def _bracket_end(s: str, i: int) -> int:
    """The position after the ``[...]`` at ``i``, or -1 when it is never
    closed (then it is not an optional argument at all)."""
    depth, n, k = 0, len(s), i + 1
    while k < n:
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return -1
        elif c == "]" and depth == 0:
            return k + 1
        k += 1
    return -1


def _skip_blanks(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _env_name(s: str, i: int) -> tuple[str | None, int]:
    """The environment name in the ``{...}`` after \\begin or \\end at ``i``,
    and the position after its closing brace."""
    k = _skip_blanks(s, i)
    if k >= len(s) or s[k] != "{":
        return None, i
    close = s.find("}", k)
    if close < 0:
        return None, i
    name = s[k + 1 : close].strip()
    if not _ENV_NAME.fullmatch(name):
        return None, i
    return name, close + 1


def _env_close(s: str, name: str, i: int) -> tuple[int, int]:
    """Where the environment ``name`` opened before ``i`` ends, as the start of its
    \\end and the position after it, counting environments of the same name
    nested inside. Both are the end of ``s`` when it never ends."""
    begin, end = "\\begin{" + name + "}", "\\end{" + name + "}"
    depth = 1
    while True:
        k = _find(s, end, i)
        if k < 0:
            return len(s), len(s)
        # Every \begin of the same name before this \end opens one more.
        depth += s.count(begin, i, k)
        depth -= 1
        if depth == 0:
            return k, k + len(end)
        i = k + len(end)


# --- Paragraphs ---------------------------------------------------------------------


def _document_scan(text: str) -> tuple[int, int, list[tuple[int, int]]]:
    """The part of a .tex file that is checked, and the stretches a blank
    line does not end.

    The part is everything between \\begin{document} and \\end{document} when
    the file has the first outside a comment, the whole file otherwise (a
    chapter that is \\input). The stretches are the environments that become
    one markup item (_WHOLE_ENVS), from their \\begin to the end of their \\end.
    An environment that is never closed is no stretch. It is usually one being
    typed, and the rest of the file should not turn into one paragraph while
    it is.
    """
    n = len(text)
    lo, hi = 0, n
    found_document = False
    stack: list[tuple[str, int]] = []
    regions: list[tuple[int, int]] = []
    i = 0
    while i < n:
        c = text[i]
        if c == "%":
            i = _line_end(text, i)
            continue
        if c != "\\":
            m = _BACKSLASH_OR_PERCENT.search(text, i)
            i = m.start() if m else n
            continue
        m = _LETTERS_RE.match(text, i + 1)
        if m is None:
            i += 2
            continue
        word, j = m.group(), m.end()
        if word == "verb":
            i = _verb_end(text, j)
            continue
        if word not in ("begin", "end"):
            i = j
            continue
        name, after = _env_name(text, j)
        if name is None:
            i = j
            continue
        if word == "begin":
            if name == "document" and not found_document and not stack:
                found_document, lo = True, after
            elif name in _VERBATIM_ENVS:
                # Nothing inside counts, not even a % or a \end of another
                # environment, so the end is looked for as written.
                close = text.find("\\end{" + name + "}", after)
                if close >= 0:
                    end = close + len(name) + 6
                    regions.append((i, end))
                    i = end
                    continue
            elif name in _WHOLE_ENVS:
                stack.append((name, i))
        else:
            if name == "document" and found_document and not stack:
                hi = i
                break
            if any(open_name == name for open_name, _ in stack):
                while stack:
                    open_name, start = stack.pop()
                    if open_name == name:
                        regions.append((start, after))
                        break
        i = after
    return lo, hi, regions


def _verb_end(s: str, j: int) -> int:
    """The position after a \\verb whose name ends at ``j``."""
    if j < len(s) and s[j] == "*":
        j += 1
    if j >= len(s) or s[j] in _SPACE or s[j].isalpha():
        return j
    close = s.find(s[j], j + 1, _line_end(s, j))
    return _line_end(s, j) if close < 0 else close + 1


def paragraphs(text: str, kind: str = "latex") -> list[tuple[int, int]]:
    """Each paragraph's [start, end) in ``text``, in order, as Python indices.

    A paragraph is a block of lines between blank lines (empty or only
    whitespace, so a line holding a % comment is not blank). It starts at the
    beginning of its first line and ends at the end of its last, without the
    newline. For LaTeX, only the document body counts, and a blank line inside
    a skipped or math environment does not split it.
    """
    lo, hi, found = (0, len(text), []) if kind != "latex" else _document_scan(text)
    regions: list[tuple[int, int]] = []
    for a, b in sorted(found):
        if regions and a <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(b, regions[-1][1]))
        else:
            regions.append((a, b))
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    last = 0
    r = 0
    pos = lo
    while pos < hi:
        end = text.find("\n", pos, hi)
        end = hi if end < 0 else end
        while r < len(regions) and regions[r][1] <= pos:
            r += 1
        inside = r < len(regions) and regions[r][0] < pos
        if text[pos:end].strip():
            if start is None:
                start = pos
            last = end
        elif not inside and start is not None:
            blocks.append((start, last))
            start = None
        pos = end + 1
    if start is not None:
        blocks.append((start, last))
    return blocks


# --- LaTeX to items -----------------------------------------------------------------


def _vowel_sound(math: str) -> bool:
    """Whether the math's first symbol is said with a vowel sound. Styling
    commands and braces are passed over, any other command decides by its
    name, a letter by its own name, and a number when it starts with 8 or is
    eleven or eighteen."""
    for m in _MATH_TOKEN.finditer(math):
        command, letter, number = m.groups()
        if command is not None:
            if command in _MATH_STYLES:
                continue
            return command in _MATH_VOWELS
        if letter is not None:
            return letter.lower() in _VOWEL_SOUNDS
        if number is not None:
            return number.startswith("8") or number in ("11", "18")
    return False


def _code_sound(code: str) -> bool | None:
    """Whether code or a URL starts with a vowel sound, None when that depends
    on whether it is read as a word or as letters."""
    m = _CODE_WORD.search(_COMMANDS_IN_CODE.sub("", code))
    if m is None:
        return None
    word = m.group()
    first = word[0].lower()
    if first.isdigit():
        return word.startswith("8") or word in ("11", "18")
    if first in "aeio":
        return True
    if first in _CODE_CONSONANTS:
        return False
    return None


def _accented(base: str, mark: str) -> str:
    base = base.strip("{} \t")
    if base in ("\\i", "\\j"):
        base = base[1]
    base = _DOTLESS.get(base, base)
    if not base or not base[0].isalpha():
        return ""
    return unicodedata.normalize("NFC", base[0] + mark) + base[1:]


class _Annotator:
    """Reads one paragraph of LaTeX and writes items. Groups are read
    recursively, so \\emph{a \\textbf{b}} keeps both words."""

    def __init__(self, s: str) -> None:
        self.s = s
        self.n = len(s)
        self.items: list[Item] = []
        self.count = 0
        self.lines: dict[int, bool] = {}

    def add(self, a: int, b: int, interp: str | None = None) -> None:
        if b > a:
            self.items.append((self.s[a:b], interp))

    def dummy(self, i: int, vowel: bool | None, display: str | None = None, number: bool = False) -> str:
        """The dummy word for markup starting at ``i``. ``vowel`` says whether
        it is said with a vowel sound, None when nothing says. Then it agrees
        with the article before it, so "a" or "an" is never flagged for
        something that cannot be pronounced. ``display`` is display math.
        ``number`` makes it a number, 10<n> or 80<n> ("eight hundred")."""
        if vowel is None:
            vowel = _AN_BEFORE.search(self.s, max(0, i - 40), i) is not None
        # Numbered per paragraph, so two in a row are two different words and
        # not "a repeated word".
        self.count += 1
        if number:
            return ("80" if vowel else "10") + str(self.count)
        word = ("Ina" if vowel else "Dummy") + str(self.count)
        if display is not None:
            body = display.rstrip()
            while True:
                m = _DISPLAY_TAIL.search(body)
                if m is None:
                    break
                body = body[: m.start()].rstrip()
            # The sentence ends (or pauses) in the display, and so it does
            # after the dummy. "\[ x = 1. \] Then" reads "Dummy1. Then".
            if body.endswith((".", ",")):
                word += body[-1]
        return word

    def prose_line(self, i: int) -> bool:
        """Whether the line around ``i`` has prose on it, anything besides
        commands, braces, brackets, whitespace and a comment."""
        start = self.s.rfind("\n", 0, i) + 1
        if start not in self.lines:
            line = self.s[start : _line_end(self.s, i)]
            comment = _find(line, "%", 0)
            line = line if comment < 0 else line[:comment]
            self.lines[start] = bool(_NOT_PROSE.sub("", line))
        return self.lines[start]

    def run(self) -> list[Item]:
        i = 0
        while i < self.n:
            i = self.prose(i, 0)
            if i < self.n:
                # A closing brace with no opening one.
                self.add(i, i + 1, "")
                i += 1
        return self.items

    def prose(self, i: int, depth: int) -> int:
        """Read text from ``i`` up to the end or to a closing brace that has
        no opening one in here, and return where that is."""
        s, n = self.s, self.n
        start = i
        while True:
            m = _SPECIAL.search(s, i)
            if m is None:
                self.add(start, n)
                return n
            i = m.start()
            c = s[i]
            if (c in "-`" and not s.startswith(c * 2, i)) or (c == "'" and not s.startswith("''", i)):
                i += 1
                continue
            self.add(start, i)
            if c == "}":
                return i
            i = self.special(i, depth)
            start = i

    def special(self, i: int, depth: int) -> int:
        s = self.s
        c = s[i]
        if c == "{":
            return self.group(i, i + 1, depth, "", "")
        if c == "%":
            # The comment is markup, its newline stays text.
            end = _line_end(s, i)
            self.add(i, end, "")
            return end
        if c == "~":
            self.add(i, i + 1, " ")
            return i + 1
        if c == "-":
            if s.startswith("---", i):
                self.add(i, i + 3, "\u2014")
                return i + 3
            self.add(i, i + 2, "\u2013")
            return i + 2
        if c == "`":
            self.add(i, i + 2, "\u201c")
            return i + 2
        if c == "'":
            self.add(i, i + 2, "\u201d")
            return i + 2
        if c == "$":
            if s.startswith("$$", i):
                return self.math(i, i + 2, _find(s, "$$", i + 2), 2, True)
            return self.math(i, i + 1, _find(s, "$", i + 1), 1, False)
        return self.command(i, depth)

    def group(self, i: int, body: int, depth: int, opening: str, closing: str) -> int:
        """A group whose opening (from ``i`` to ``body``) is markup standing
        for ``opening``, its content prose, and its closing brace markup
        standing for ``closing``."""
        if depth >= _MAX_DEPTH:
            end = _group_end(self.s, body - 1)
            self.add(i, end, "")
            return end
        self.add(i, body, opening)
        k = self.prose(body, depth + 1)
        if k < self.n:
            self.add(k, k + 1, closing)
            return k + 1
        return k

    def math(self, i: int, body: int, close: int, width: int, display: bool) -> int:
        # Math that is never closed takes the rest of the paragraph. That is
        # usually math being typed, and none of it is prose.
        end = self.n if close < 0 else close + width
        math = self.s[body : close if close >= 0 else self.n]
        word = self.dummy(i, _vowel_sound(math), math if display else None)
        if not display:
            # Letters glued to the math are said with it. The $i$th element
            # is "the Ina1 element", where "Ina1th" would be a word with a
            # number in it.
            m = _LETTERS_RE.match(self.s, end)
            end = m.end() if m else end
        self.add(i, end, word)
        return end

    def args(self, j: int, raw: bool = False) -> int:
        """The position after the [...] and {...} arguments from ``j``, which
        may be separated by spaces but not by a line break."""
        s, n = self.s, self.n
        while True:
            k = _skip_blanks(s, j)
            if k < n and s[k] == "[":
                end = _bracket_end(s, k)
                if end < 0:
                    return j
                j = end
            elif k < n and s[k] == "{":
                j = _group_end(s, k, raw)
            else:
                return j

    def optionals(self, j: int) -> int:
        """The position after the [...] arguments from ``j``."""
        while True:
            k = _skip_blanks(self.s, j)
            if k >= self.n or self.s[k] != "[":
                return j
            end = _bracket_end(self.s, k)
            if end < 0:
                return j
            j = end

    def command(self, i: int, depth: int) -> int:
        s, n = self.s, self.n
        if i + 1 >= n:
            self.add(i, n, "")
            return n
        m = _LETTERS_RE.match(s, i + 1)
        if m is None:
            return self.symbol(i, s[i + 1])
        name, j = m.group(), m.end()
        if name == "verb":
            end = _verb_end(s, j)
            self.add(i, end, self.dummy(i, _code_sound(s[j:end])) if end > j else "")
            return end
        if name == "begin":
            return self.begin(i, j)
        if name == "end":
            _, after = _env_name(s, j)
            self.add(i, after, "")
            return after
        if name in _LETTER_ACCENTS:
            return self.accent(i, j, _ACCENTS[name], letter=True)
        if name in _LETTERS:
            # TeX swallows the spaces after a control word, which is how a
            # letter command sits inside a word. "stra\ss e" is "straße".
            end = _skip_blanks(s, j)
            self.add(i, end, _DOTLESS.get(_LETTERS[name], _LETTERS[name]))
            return end
        if name in _WORDS:
            word = _WORDS[name]
            # \etc. prints one full stop, not two.
            if word.endswith(".") and s.startswith(".", j):
                word = word[:-1]
            self.add(i, j, word)
            return j
        if j < n and s[j] == "*":
            j += 1
        if name in _DUMMY_COMMANDS:
            end = self.args(j, raw=name in _RAW_ARGS)
            sound = None
            if name in _CODE_COMMANDS:
                k = s.find("{", j, end)
                sound = _code_sound(s[k + 1 : _group_end(s, k, raw=True) - 1]) if k >= 0 else None
            self.add(i, end, self.dummy(i, sound))
            return end
        if name in _SPOKEN_COMMANDS:
            end = self.args(j)
            k = s.find("{", j, end)
            self.add(i, end, self.dummy(i, _vowel_sound(s[k + 1 : _group_end(s, k) - 1] if k >= 0 else "")))
            return end
        if name == "href":
            k = _skip_blanks(s, j)
            if k < n and s[k] == "{":
                k = _skip_blanks(s, _group_end(s, k, raw=True))
                if k < n and s[k] == "{":
                    return self.group(i, k + 1, depth, "", "")
            end = self.args(j, raw=True)
            k = s.find("{", j, end)
            self.add(i, end, self.dummy(i, _code_sound(s[k + 1 : end]) if k >= 0 else None))
            return end
        if name in _PROSE or name in _HEADINGS:
            if name == "textcolor":
                j = self.optionals(j)
                k = _skip_blanks(s, j)
                if k < n and s[k] == "{":
                    j = _group_end(s, k)
            j = self.optionals(j)
            k = _skip_blanks(s, j)
            if k < n and s[k] == "{":
                opening, closing = _BRACES.get(name, ("", "\n\n" if name in _HEADINGS else ""))
                return self.group(i, k + 1, depth, opening, closing)
            self.add(i, j, "")
            return j
        if name == "item":
            # A bullet, which LanguageTool reads as a list. Items that are
            # not sentences ("a new method;") are not flagged for starting
            # with a small letter.
            end = self.optionals(j)
            self.add(i, end, "\u2022")
            return end
        # Anything else with an argument prints nothing that could be checked:
        # \label, \input, \vspace{..}, \todo{..}. Without one it is either
        # invisible (\centering) or the paper's own macro for a number or a
        # name, which stands in the sentence like a word. It reads as a number,
        # because "All Dummy1 checks of Dummy2 runs passed" is a noun and a
        # verb to LanguageTool ("did you mean past?"), and numbers are what
        # these macros mostly are.
        end = self.args(j)
        if name not in _INVISIBLE and _EMPTY_ARGS.fullmatch(s, j, end) and self.prose_line(i):
            self.add(i, end, self.dummy(i, None, number=True))
        else:
            self.add(i, end, "")
        return end

    def symbol(self, i: int, c: str) -> int:
        s, j = self.s, i + 2
        if c == "\\":
            # A line break, with its optional * and [length].
            if j < self.n and s[j] == "*":
                j += 1
            if j < self.n and s[j] == "[":
                end = _bracket_end(s, j)
                j = j if end < 0 else end
            self.add(i, j, " ")
            return j
        if c == "(":
            return self.math(i, j, _find(s, "\\)", j), 2, False)
        if c == "[":
            return self.math(i, j, _find(s, "\\]", j), 2, True)
        if c in _ACCENTS:
            return self.accent(i, j, _ACCENTS[c], letter=False)
        self.add(i, j, _SYMBOLS.get(c, ""))
        return j

    def accent(self, i: int, j: int, mark: str, letter: bool) -> int:
        """\\"a, \\"{a}, \\'\\i, \\c{c}, \\c c: the composed letter."""
        s, n = self.s, self.n
        k = _skip_blanks(s, j)
        if k < n and s[k] == "{":
            end = _group_end(s, k)
            base = s[k + 1 : end - 1] if s[end - 1 : end] == "}" else s[k + 1 : end]
        elif k < n and s[k] == "\\" and s[k + 1 : k + 2] in ("i", "j") and not s[k + 2 : k + 3].isalpha():
            end, base = _skip_blanks(s, k + 2), s[k : k + 2]
        elif k < n and s[k].isalpha() and (k > j or not letter):
            end, base = k + 1, s[k]
        else:
            self.add(i, j, "")
            return j
        self.add(i, end, _accented(base, mark))
        return end

    def begin(self, i: int, j: int) -> int:
        s = self.s
        name, after = _env_name(s, j)
        if name is None:
            self.add(i, j, "")
            return j
        if name in _VERBATIM_ENVS:
            close = s.find("\\end{" + name + "}", after)
            end = self.n if close < 0 else close + len(name) + 6
            self.add(i, end, "")
            return end
        if name in _SKIPPED_ENVS or name in _MATH_ENVS:
            close, end = _env_close(s, name, after)
            math = s[after:close]
            self.add(i, end, "" if name in _SKIPPED_ENVS else self.dummy(i, _vowel_sound(math), math))
            return end
        # Any other environment (itemize, figure, abstract, theorem) keeps its
        # content. \begin{x} and its arguments print nothing.
        end = self.args(after)
        self.add(i, end, "")
        return end


def _tidy(items: list[Item]) -> list[Item]:
    """The items with the whitespace that markup leaves behind folded away.

    A command that prints nothing between two spaces leaves two spaces, which
    LanguageTool reports (CONSECUTIVE_SPACES), and a line of nothing but
    \\label or \\centering leaves an empty line, which it reads as a paragraph
    break. LaTeX itself swallows both. So whitespace after markup that
    already follows whitespace becomes part of that markup, and so do spaces
    left before a full stop or comma by a command that printed nothing. Only
    which item a character belongs to changes, never the characters.

    Indentation is markup too, at the start of the paragraph and of every
    line. LanguageTool reports spaces after a line break (WHITESPACE_RULE),
    and LaTeX sources are indented.
    """
    split: list[Item] = []
    for n, (src, interp) in enumerate(items):
        if interp is not None:
            split.append((src, interp))
            continue
        spans: list[tuple[int, int]] = []
        if n == 0 and (m := _LEADING.match(src)):
            spans.append(m.span())
        spans += [m.span() for m in _INDENT.finditer(src) if not spans or m.start() >= spans[0][1]]
        pos = 0
        for a, b in spans:
            if a > pos:
                split.append((src[pos:a], None))
            split.append((src[a:b], ""))
            pos = b
        if pos < len(src):
            split.append((src[pos:], None))
    out: list[list] = []
    tail = "\n"  # the last character LanguageTool reads, so far
    for src, interp in split:
        if interp is not None:
            if tail in _SPACE:
                interp = interp.lstrip(" \t")
            elif _DUMMY_WORD.match(interp) and tail.isalnum():
                # "text\cite{x}" still has a space before its dummy.
                interp = " " + interp
            if interp and interp[0] in _CLOSERS and tail in _SPACE:
                # "\footnote{See X. }" would read "(See X. )" otherwise.
                tail = _pull_space(out) or tail
            if out and out[-1][1] is not None:
                out[-1][0] += src
                out[-1][1] += interp
            else:
                out.append([src, interp])
            if interp:
                tail = interp[-1]
            continue
        if out and out[-1][1] is not None and src and (tail in _SPACE or tail in _OPENERS):
            # After an opening bracket too: "\footnote{\raggedright Text"
            # reads "(Text".
            lead = len(src) - len(src.lstrip(_SPACE))
            out[-1][0] += src[:lead]
            src = src[lead:]
            if src and src[0] in _CLOSING_PUNCTUATION and out[-1][1] == "" and tail in _SPACE:
                # "said \foo{x}." would read "said ." otherwise.
                tail = _pull_space(out) or tail
        if not src:
            continue
        if out and out[-1][1] is None:
            out[-1][0] += src
        else:
            out.append([src, None])
        tail = src[-1]
    return [(src, interp) for src, interp in out]


def _pull_space(out: list[list]) -> str | None:
    """Move the whitespace at the end of the last text item into the markup
    after it (past one that stands for nothing), and return the character
    now read last. None when there is no such whitespace to move."""
    k = len(out) - 1
    if k >= 0 and out[k][1] == "":
        k -= 1
    if k < 0 or out[k][1] is not None:
        return None
    before = out[k][0]
    kept = before.rstrip(_SPACE)
    if not kept or kept == before:
        return None
    out[k][0] = kept
    if k + 1 < len(out):
        out[k + 1][0] = before[len(kept) :] + out[k + 1][0]
    else:
        out.append([before[len(kept) :], ""])
    return kept[-1]


def annotate(source: str, kind: str = "latex") -> list[Item]:
    """One paragraph as items whose sources, joined, are ``source`` exactly.
    ``kind`` "text" is plain prose, one text item."""
    if kind != "latex":
        return [(source, None)] if source else []
    return _tidy(_Annotator(source).run())


def has_text(items: list[Item]) -> bool:
    """Whether there is anything to check, a text item with more than
    whitespace in it."""
    return any(interp is None and src.strip() for src, interp in items)


def annotation(items: list[Item]) -> list[dict[str, str]]:
    """The items in LanguageTool's JSON shape. Markup that stands for nothing
    is sent without interpretAs, which LanguageTool reads as empty."""
    out: list[dict[str, str]] = []
    for src, interp in items:
        if interp is None:
            out.append({"text": src})
        elif interp:
            out.append({"markup": src, "interpretAs": interp})
        else:
            out.append({"markup": src})
    return out


def interpreted(items: list[Item]) -> str:
    """What LanguageTool reads, the text and the markup as what it stands for.
    For the tests and for looking at what a paragraph turns into."""
    return "".join(src if interp is None else interp for src, interp in items)
