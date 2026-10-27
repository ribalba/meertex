"""LaTeX as text, names as bibtex splits them, and \\cite commands
(docs/DESIGN.md §16).

Titles and names are compared with the publishers' records after LaTeX is
turned into text, so ``Erd{\\H o}s`` and ``Erdős`` are the same name. ``fold``
then drops case, accents and punctuation, which the comparison ignores.

``cites`` finds the citation commands of a .tex file outside comments and
verbatim text, and ``sentence_at`` gives the sentence a citation sits in, for
showing it and for asking whether the cited source supports it.
"""

import difflib
import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import lru_cache

# The combining mark of every accent command.
_ACCENTS = {
    '"': "\u0308",
    "'": "\u0301",
    "`": "\u0300",
    "^": "\u0302",
    "~": "\u0303",
    "=": "\u0304",
    ".": "\u0307",
    "u": "\u0306",
    "v": "\u030c",
    "H": "\u030b",
    "c": "\u0327",
    "k": "\u0328",
    "r": "\u030a",
    "d": "\u0323",
    "b": "\u0331",
    "t": "\u0361",
}
_TIE = "\u0361"
_LETTERS = {
    "ss": "\u00df",
    "aa": "\u00e5",
    "AA": "\u00c5",
    "o": "\u00f8",
    "O": "\u00d8",
    "ae": "\u00e6",
    "AE": "\u00c6",
    "oe": "\u0153",
    "OE": "\u0152",
    "l": "\u0142",
    "L": "\u0141",
    "i": "\u0131",
    "j": "\u0237",
    "dh": "\u00f0",
    "DH": "\u00d0",
    "th": "\u00fe",
    "TH": "\u00de",
    "dj": "\u0111",
    "DJ": "\u0110",
}
# An accent on a dotless i or j goes on the plain letter.
_DOTLESS = {"\u0131": "i", "\u0237": "j"}
_WORDS = {
    "LaTeX": "LaTeX",
    "TeX": "TeX",
    "LaTeXe": "LaTeX2e",
    "BibTeX": "BibTeX",
    "textendash": "\u2013",
    "textemdash": "\u2014",
    "textquoteright": "\u2019",
    "textquoteleft": "\u2018",
    "textquotedblleft": "\u201c",
    "textquotedblright": "\u201d",
    "dots": "\u2026",
    "ldots": "\u2026",
    "textellipsis": "\u2026",
    "textbackslash": "\\",
    "textasciitilde": "~",
    "textunderscore": "_",
    "textbar": "|",
    "textless": "<",
    "textgreater": ">",
    "copyright": "\u00a9",
    "textcopyright": "\u00a9",
    "textregistered": "\u00ae",
    "texttrademark": "\u2122",
    "S": "\u00a7",
    "P": "\u00b6",
    "pounds": "\u00a3",
    "euro": "\u20ac",
    "textdegree": "\u00b0",
    "guillemotleft": "\u00ab",
    "guillemotright": "\u00bb",
    "quad": " ",
    "qquad": " ",
    "enspace": " ",
    "space": " ",
    "slash": "/",
}
_GLUED = frozenset(
    {"textendash", "textemdash", "textquoteright", "textquoteleft", "textquotedblleft", "textquotedblright"}
)
# Commands whose arguments are not text. They go with their arguments.
_DROPPED = frozenset(
    {
        "label",
        "index",
        "vspace",
        "hspace",
        "includegraphics",
        "input",
        "include",
        "bibliography",
        "bibliographystyle",
        "addbibresource",
    }
)
# In math these only change the font or the size, so they leave no text.
_MATH_SILENT = frozenset(
    {
        "mathcal",
        "mathrm",
        "mathbf",
        "mathit",
        "mathsf",
        "mathtt",
        "mathbb",
        "mathfrak",
        "mathnormal",
        "boldsymbol",
        "bm",
        "text",
        "textrm",
        "textit",
        "textbf",
        "textsf",
        "texttt",
        "operatorname",
        "mbox",
        "left",
        "right",
        "big",
        "Big",
        "bigg",
        "Bigg",
        "bigl",
        "bigr",
        "Bigl",
        "Bigr",
        "displaystyle",
        "textstyle",
        "scriptstyle",
    }
)
_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[\W_]+")
_FOLD = str.maketrans(
    {
        "\u00df": "ss",
        "\u00f8": "o",
        "\u00e6": "ae",
        "\u0153": "oe",
        "\u0142": "l",
        "\u0111": "d",
        "\u00fe": "th",
        "\u00f0": "d",
        "\u0131": "i",
        "\u0237": "j",
        "\u0127": "h",
        "\u0167": "t",
    }
)


class _Converter:
    """Reads LaTeX source and writes plain text. Groups are read recursively,
    so an accent command can take a group as its argument."""

    def __init__(self, s: str) -> None:
        self.s = s
        self.n = len(s)

    def read(self, i: int, in_group: bool, math: bool) -> tuple[str, int]:
        s = self.s
        out: list[str] = []
        while i < self.n:
            c = s[i]
            if c == "}":
                if in_group:
                    return "".join(out), i + 1
                i += 1
            elif c == "{":
                text, i = self.read(i + 1, True, math)
                out.append(text)
            elif c == "\\":
                text, i = self.command(i, math)
                out.append(text)
            elif c == "$" and not math:
                text, i = self.math(i)
                out.append(text)
            elif c == "~":
                out.append(" ")
                i += 1
            elif c == "-" and not math and s.startswith("--", i):
                if s.startswith("---", i):
                    out.append("\u2014")
                    i += 3
                else:
                    out.append("\u2013")
                    i += 2
            elif c == "`" and not math:
                if s.startswith("``", i):
                    out.append("\u201c")
                    i += 2
                else:
                    out.append("\u2018")
                    i += 1
            elif c == "'" and not math and s.startswith("''", i):
                out.append("\u201d")
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out), i

    def math(self, i: int) -> tuple[str, int]:
        """The content of ``$...$`` or ``$$...$$`` at ``i``, without the dollars."""
        s = self.s
        width = 2 if s.startswith("$$", i) else 1
        close = i + width
        while True:
            close = s.find("$" * width, close)
            if close < 0 or s[close - 1] != "\\":
                break
            close += 1
        end = self.n if close < 0 else close
        text, _ = _Converter(s[i + width : end]).read(0, False, True)
        return text, self.n if close < 0 else close + width

    def skip_spaces(self, i: int) -> int:
        while i < self.n and self.s[i] in " \t\r\n":
            i += 1
        return i

    def group_raw(self, i: int) -> tuple[str, int]:
        """The text of the group at ``i`` as written, for URLs."""
        depth = 0
        for j in range(i, self.n):
            if self.s[j] == "{":
                depth += 1
            elif self.s[j] == "}":
                depth -= 1
                if depth == 0:
                    return self.s[i + 1 : j], j + 1
        return self.s[i + 1 :], self.n

    def optional(self, i: int) -> int:
        """The position after an optional argument ``[...]`` at ``i``, or ``i``."""
        j = self.skip_spaces(i)
        if j < self.n and self.s[j] == "[":
            close = self.s.find("]", j)
            if close > 0:
                return close + 1
        return i

    def command(self, i: int, math: bool) -> tuple[str, int]:
        s = self.s
        if i + 1 >= self.n:
            return "", self.n
        c = s[i + 1]
        if not ("a" <= c <= "z" or "A" <= c <= "Z"):
            j = i + 2
            if c in _ACCENTS and not math:
                return self.accent(_ACCENTS[c], j, math)
            if c in "&%$#_{}|":
                return c, j
            if c in " \t\r\n\\,;:>":
                return " ", j
            return "", j

        j = i + 2
        while j < self.n and ("a" <= s[j] <= "z" or "A" <= s[j] <= "Z"):
            j += 1
        name = s[i + 1 : j]
        if math:
            return ("" if name in _MATH_SILENT else name), j
        if name in _ACCENTS:
            return self.accent(_ACCENTS[name], j, math)
        if name in _LETTERS:
            # TeX swallows the spaces after a control word, so ``Stra\ss e`` is
            # one word.
            return _LETTERS[name], self.skip_spaces(j)
        if name in _WORDS:
            # Dashes and quotes attach to the next word, as in TeX. Words such
            # as \LaTeX keep the space after them, which is what the writer
            # meant even when TeX would swallow it.
            return _WORDS[name], self.skip_spaces(j) if name in _GLUED else j
        if j < self.n and s[j] == "*":
            j += 1
        if name == "url":
            k = self.skip_spaces(j)
            if k < self.n and s[k] == "{":
                return self.group_raw(k)
            return "", j
        if name == "href":
            k = self.skip_spaces(j)
            if k < self.n and s[k] == "{":
                _, k = self.group_raw(k)
                return "", k
            return "", j
        if name in _DROPPED:
            k = self.optional(j)
            k = self.skip_spaces(k)
            if k < self.n and s[k] == "{":
                _, k = self.group_raw(k)
                return "", k
            return "", j
        # Any other command goes, and its argument is read as text.
        return "", j

    def accent(self, mark: str, j: int, math: bool) -> tuple[str, int]:
        j = self.skip_spaces(j)
        if j >= self.n:
            return "", j
        if self.s[j] == "{":
            arg, j = self.read(j + 1, True, math)
        elif self.s[j] == "\\":
            arg, j = self.command(j, math)
        elif self.s[j] == "}":
            return "", j
        else:
            arg, j = self.s[j], j + 1
        arg = arg.lstrip()
        if not arg:
            return "", j
        base = _DOTLESS.get(arg[0], arg[0])
        if mark == _TIE:
            return base + mark + arg[1:], j
        return unicodedata.normalize("NFC", base + mark) + arg[1:], j


def to_text(latex: str) -> str:
    """LaTeX source as plain Unicode text, for comparing and for showing."""
    if not latex:
        return ""
    try:
        text, _ = _Converter(latex).read(0, False, False)
    except RecursionError:
        # Braces nested deeper than Python's stack. Rough text is better
        # than none.
        text = re.sub(r"\\[A-Za-z]+|[\\{}$]", " ", latex)
    return unicodedata.normalize("NFC", _SPACES.sub(" ", text).strip())


def fold(s: str) -> str:
    """Text for comparing. Case, accents and punctuation do not count, and
    every run of other characters is one space."""
    text = unicodedata.normalize("NFKD", to_text(s).casefold())
    text = "".join(c for c in text if not unicodedata.combining(c)).translate(_FOLD)
    return _NON_WORD.sub(" ", text).strip()


def words(s: str) -> list[str]:
    return fold(s).split()


def similarity(a: str, b: str) -> float:
    """How alike two titles are, from 0 to 1, after folding."""
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return 0.0
    if fa == fb:
        return 1.0
    # autojunk would treat common letters of a long title as junk and make
    # the ratio meaningless.
    return difflib.SequenceMatcher(None, fa, fb, autojunk=False).ratio()


def is_prefix_title(a: str, b: str) -> bool:
    """Whether one title is the other without its end, as with a missing
    subtitle. The shorter one needs at least 3 words, and equal titles are
    not prefixes."""
    wa, wb = words(a), words(b)
    short, long = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return 3 <= len(short) < len(long) and long[: len(short)] == short


@dataclass
class Name:
    first: str
    von: str
    last: str
    jr: str
    literal: bool = False

    @property
    def family(self) -> str:
        return (self.von + " " + self.last).strip()


def _split_top(s: str, separator) -> list[str]:
    """``s`` split where ``separator(c)`` holds outside braces."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for c in s:
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(depth - 1, 0)
        if depth == 0 and separator(c):
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
    parts.append("".join(current))
    return parts


def _top_words(s: str) -> list[str]:
    return [w for w in _split_top(s, str.isspace) if w]


def split_names(value: str) -> tuple[list[str], bool]:
    """The names of a bibtex name list, and whether it ended in ``and others``."""
    names: list[str] = []
    current: list[str] = []
    for word in _top_words(value):
        if word.lower() == "and":
            names.append(" ".join(current))
            current = []
        else:
            current.append(word)
    names.append(" ".join(current))
    names = [n.strip() for n in names if n.strip()]
    others = bool(names) and names[-1].casefold() == "others"
    if others:
        names.pop()
    return names, others


def _is_lower(word: str) -> bool:
    """bibtex's test for a von word. Its first letter outside braces is
    lowercase. A brace group that starts with a command is a special
    character and counts with the letter it makes."""
    depth = 0
    i = 0
    n = len(word)
    while i < n:
        c = word[i]
        if c == "{":
            if depth == 0 and word.startswith("{\\", i):
                return _special_is_lower(word[i + 1 :])
            depth += 1
        elif c == "}":
            depth = max(depth - 1, 0)
        elif c == "\\" and depth == 0:
            return _special_is_lower(word[i:])
        elif depth == 0 and c.isalpha():
            return c.islower()
        i += 1
    return False


def _special_is_lower(s: str) -> bool:
    m = re.match(r"\\([A-Za-z]+)", s)
    if m and m.group(1) in _LETTERS:
        return m.group(1)[0].islower()
    start = m.end() if m else 2
    for c in s[start:]:
        if c.isalpha():
            return c.islower()
    return False


def _von_last(words_: list[str], von_start: int) -> tuple[list[str], list[str]]:
    """bibtex's split of the von and last words. The von part ends with the
    last lowercase word, and Last keeps at least one word."""
    von_end = len(words_) - 1
    while von_end > von_start and not _is_lower(words_[von_end - 1]):
        von_end -= 1
    return words_[von_start:von_end], words_[von_end:]


def parse_name(name: str) -> Name:
    """A name split into First, von, Last and Jr by bibtex's rules. The parts
    keep their LaTeX."""
    s = name.strip()
    if not s:
        return Name("", "", "", "")
    if s.startswith("{") and _group_end(s, 0) == len(s):
        return Name("", "", s[1:-1].strip(), "", literal=True)
    parts = [" ".join(_top_words(p)) for p in _split_top(s, lambda c: c == ",")]
    if len(parts) == 1:
        ws = _top_words(parts[0])
        if len(ws) == 1:
            return Name("", "", ws[0], "")
        von_start = next((i for i in range(len(ws) - 1) if _is_lower(ws[i])), len(ws) - 1)
        von, last = _von_last(ws, von_start)
        return Name(" ".join(ws[:von_start]), " ".join(von), " ".join(last), "")
    von, last = _von_last(_top_words(parts[0]), 0)
    if len(parts) == 2:
        jr, first = "", parts[1]
    else:
        jr, first = parts[1], ", ".join(parts[2:])
    return Name(first, " ".join(von), " ".join(last), jr)


# Generational suffixes written as part of the last name ("King Jr.").
_SUFFIXES = frozenset({"jr", "sr", "iii", "iv"})


def family_key(name: str) -> str:
    """The word two spellings of the same family name share. It is the last
    word of the folded Last part, so ``van der Aalst`` and ``Aalst`` give
    ``aalst``, and ``García-Molina`` gives ``molina``."""
    if not name or not name.strip():
        return ""
    n = parse_name(name)
    found = fold(n.last).split()
    if not found or found[-1] in _SUFFIXES:
        whole = " ".join(p for p in (n.first, n.von, n.last) if p)
        found = fold(whole).split() or fold(name).split()
        while len(found) > 1 and found[-1] in _SUFFIXES:
            found.pop()
    return found[-1] if found else ""


def _group_end(s: str, i: int) -> int:
    """The position after the brace that closes the one at ``i``, or -1."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


@dataclass
class Cite:
    key: str
    path: str
    line: int
    start: int
    end: int
    command: str


_CITE_BASE = (
    "cite",
    "nocite",
    "citep",
    "citet",
    "citealp",
    "citealt",
    "citeauthor",
    "citeyear",
    "citeyearpar",
    "citenum",
    "citetitle",
    "citedate",
    "citeurl",
    "parencite",
    "textcite",
    "autocite",
    "footcite",
    "footcitetext",
    "smartcite",
    "supercite",
    "fullcite",
)
CITE_COMMANDS = frozenset(_CITE_BASE) | frozenset(c[0].upper() + c[1:] for c in _CITE_BASE if c != "nocite")
_CONTROL_WORD = re.compile(r"\\([A-Za-z]+)(\*?)")
# Text in these environments is not LaTeX. The comment environment is the
# comment package's.
_SKIPPED = re.compile(
    r"%|\\verb(?![A-Za-z])\*?|\\begin\s*\{(verbatim\*?|Verbatim\*?|lstlisting|minted|comment)\}"
)
_NOT_NEWLINE = re.compile(r"[^\n]")


def _escaped(s: str, i: int) -> bool:
    """Whether the character at ``i`` follows an odd number of backslashes."""
    k = i
    while k > 0 and s[k - 1] == "\\":
        k -= 1
    return (i - k) % 2 == 1


def _blank(s: str) -> str:
    return _NOT_NEWLINE.sub(" ", s)


def _mask(text: str) -> str:
    """``text`` with comments and verbatim text turned into spaces. Every
    other character stays where it is, so offsets and lines still match. A
    comment takes its newline with it, as in TeX."""
    pieces: list[str] = []
    pos = 0
    search = 0
    n = len(text)
    while True:
        m = _SKIPPED.search(text, search)
        if m is None:
            break
        if _escaped(text, m.start()):
            search = m.end()
            continue
        token = m.group()
        if token == "%":
            eol = text.find("\n", m.start())
            stop = n if eol < 0 else eol + 1
            pieces.append(text[pos : m.start()])
            pieces.append(" " * (stop - m.start()))
        elif token.startswith("\\verb"):
            delimiter = text[m.end() : m.end() + 1]
            close = text.find(delimiter, m.end() + 1) if delimiter and not delimiter.isspace() else -1
            if close < 0 or "\n" in text[m.end() : close]:
                search = m.end()
                continue
            stop = close + 1
            pieces.append(text[pos : m.start()])
            pieces.append(_blank(text[m.start() : stop]))
        else:
            # The \begin and \end stay, so the environment still ends a sentence.
            end = re.compile(r"\\end\s*\{" + re.escape(m.group(1)) + r"\}").search(text, m.end())
            stop = n if end is None else end.start()
            pieces.append(text[pos : m.end()])
            pieces.append(_blank(text[m.end() : stop]))
        pos = search = stop
    pieces.append(text[pos:])
    return "".join(pieces)


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _bracket_end(s: str, i: int) -> int:
    """The position after the ``]`` that closes the ``[`` at ``i``, or -1.
    Brackets inside braces do not count."""
    depth = 0
    for j in range(i + 1, len(s)):
        c = s[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "]" and depth <= 0:
            return j + 1
    return -1


def _scan_cites(masked: str) -> list[tuple[int, int, str, tuple[str, ...]]]:
    """``(start, end, command, keys)`` of every citation command."""
    found = []
    n = len(masked)
    for m in _CONTROL_WORD.finditer(masked):
        if m.group(1) not in CITE_COMMANDS or _escaped(masked, m.start()):
            continue
        j = _skip_ws(masked, m.end())
        for _ in range(2):
            if j < n and masked[j] == "[":
                close = _bracket_end(masked, j)
                if close < 0:
                    break
                j = _skip_ws(masked, close)
        if j >= n or masked[j] != "{":
            continue
        close = _group_end(masked, j)
        if close < 0:
            continue
        keys = tuple(k.strip() for k in masked[j + 1 : close - 1].split(",") if k.strip())
        found.append((m.start(), close, m.group(1) + m.group(2), keys))
    return found


# A sentence ends with . ! or ?, maybe closing quotes or brackets after it,
# and then white space or the end of the text.
_SENTENCE_END = re.compile(r"[.!?][)\]}'\"\u2019\u201d]*(?=\s|\Z)")
_BLANK_LINE = re.compile(r"\n[^\S\n]*\n")
_STRUCTURE = re.compile(
    r"\\(section|subsection|subsubsection|paragraph|chapter|caption|item|begin|end)(?![A-Za-z])\*?"
)
_HEADINGS = frozenset({"section", "subsection", "subsubsection", "paragraph", "chapter"})
# Words after which a period does not end the sentence, lowercased.
_ABBREVIATIONS = frozenset(
    {
        "e.g",
        "i.e",
        "al",
        "cf",
        "vs",
        "fig",
        "figs",
        "eq",
        "eqs",
        "sec",
        "secs",
        "tab",
        "tabs",
        "no",
        "nos",
        "approx",
        "resp",
        "ref",
        "refs",
        "vol",
        "p",
        "pp",
        "ch",
        "chap",
        "ca",
        "viz",
        "dr",
        "prof",
        "mr",
        "mrs",
        "ms",
    }
)
_WORD_BEFORE = re.compile(r"(?:[^\W\d_]|\.)*\Z")
_DOTTED = re.compile(r"(?:[A-Za-z]\.)+[A-Za-z]")


def _abbreviation(masked: str, period: int) -> bool:
    """Whether the period at ``period`` closes an abbreviation or an initial
    instead of a sentence."""
    word = _WORD_BEFORE.search(masked, max(0, period - 40), period).group()
    if not word:
        return False
    if word.casefold() in _ABBREVIATIONS:
        return True
    # "J. Smith", and dotted short forms such as "U.S.".
    return (len(word) == 1 and word.isupper()) or bool(_DOTTED.fullmatch(word))


def _head_end(masked: str, m: re.Match) -> int:
    """Where the text after a structural command starts. A heading's title
    belongs to the command. A caption's text does not."""
    name = m.group(1)
    j = m.end()
    if name in ("begin", "end"):
        k = _skip_ws(masked, j)
        if k < len(masked) and masked[k] == "{":
            j = max(_group_end(masked, k), k + 1)
        if name == "begin" and j < len(masked) and masked[j] == "[":
            j = max(_bracket_end(masked, j), j + 1)
        return j
    k = _skip_ws(masked, j)
    if k < len(masked) and masked[k] == "[":
        end = _bracket_end(masked, k)
        if end > 0:
            j = end
    if name == "item":
        return j
    k = _skip_ws(masked, j)
    if k < len(masked) and masked[k] == "{":
        if name == "caption":
            return k + 1
        end = _group_end(masked, k)
        return end if end > 0 else k + 1
    return j


@dataclass
class _Prepared:
    masked: str
    line_starts: list[int]
    cites: list[tuple[int, int, str, tuple[str, ...]]]
    cuts: list[int]


@lru_cache(maxsize=8)
def _prepare(text: str) -> _Prepared:
    """What ``cites`` and ``sentence_at`` need of a text. A report asks for
    many sentences of the same file, so this is kept for a few texts."""
    masked = _mask(text)
    found = _scan_cites(masked)
    cuts = {0, len(masked)}
    for m in _SENTENCE_END.finditer(masked):
        if masked[m.start()] == "." and _abbreviation(masked, m.start()):
            continue
        cuts.add(m.end())
    for m in _BLANK_LINE.finditer(masked):
        cuts.add(m.start() + 1)
    for m in _STRUCTURE.finditer(masked):
        if _escaped(masked, m.start()):
            continue
        cuts.add(m.start())
        cuts.add(_head_end(masked, m))
    # A period in a citation's optional argument does not end a sentence.
    starts = [s for s, _, _, _ in found]
    kept = []
    for c in sorted(cuts):
        i = bisect_left(starts, c) - 1
        if i < 0 or found[i][1] <= c:
            kept.append(c)
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
    return _Prepared(masked, line_starts, found, kept)


def cites(text: str, path: str = "") -> list[Cite]:
    """One Cite per key of every citation command in a .tex text, in order."""
    p = _prepare(text)
    out = []
    for start, end, command, keys in p.cites:
        line = bisect_right(p.line_starts, start)
        out.extend(Cite(key, path, line, start, end, command) for key in keys)
    return out


_CITE_OPEN = "\ue000"
_CITE_CLOSE = "\ue001"
_ANCHOR = "\ue002"
_PLACEHOLDER = re.compile(_CITE_OPEN + r"(\d+)" + _CITE_CLOSE)


def sentence_at(text: str, index: int, limit: int = 600) -> str:
    """The sentence around ``index`` as text, with its citations as
    ``[key1, key2]``. A sentence longer than ``limit`` is cut to about
    ``limit`` characters around ``index``."""
    p = _prepare(text)
    masked = p.masked
    index = min(max(index, 0), len(masked))
    k = bisect_right(p.cuts, index)
    start = p.cuts[k - 1]
    end = p.cuts[k] if k < len(p.cuts) else len(masked)
    if start >= end:
        return ""

    inside = [c for c in p.cites if c[0] >= start and c[1] <= end]
    anchor = index
    for s, e, _, _ in inside:
        if s <= anchor < e:
            anchor = s
            break
    else:
        # Keep the anchor out of the middle of a word or a command.
        while anchor > start and not masked[anchor - 1].isspace():
            anchor -= 1
        for s, e, _, _ in inside:
            if s < anchor < e:
                anchor = s

    pieces: list[str] = []
    labels: list[str] = []
    pos = start
    placed = False
    for s, e, _, keys in inside:
        if not placed and anchor <= s:
            pieces += [masked[pos:anchor], _ANCHOR]
            pos = anchor
            placed = True
        pieces.append(masked[pos:s])
        pieces.append(f"{_CITE_OPEN}{len(labels)}{_CITE_CLOSE}")
        labels.append("[" + ", ".join(keys) + "]")
        pos = e
    if not placed:
        anchor = max(anchor, pos)
        pieces += [masked[pos:anchor], _ANCHOR]
        pos = anchor
    pieces.append(masked[pos:end])

    sentence = to_text("".join(pieces))
    sentence = _PLACEHOLDER.sub(lambda m: labels[int(m.group(1))], sentence)
    at = sentence.find(_ANCHOR)
    sentence = sentence.replace(_ANCHOR, "")
    sentence = _SPACES.sub(" ", sentence).strip()
    if len(sentence) <= limit:
        return sentence
    return _window(sentence, max(at, 0), limit)


def _window(sentence: str, at: int, limit: int) -> str:
    """About ``limit`` characters of ``sentence`` around ``at``, cut at word
    boundaries, with an ellipsis where it was cut."""
    at = min(at, len(sentence))
    lo = max(0, at - limit // 2)
    hi = min(len(sentence), lo + limit)
    lo = max(0, hi - limit)
    if lo > 0 and sentence[lo - 1] != " ":
        space = sentence.find(" ", lo, at + 1)
        lo = space + 1 if space >= 0 else lo
    if hi < len(sentence) and sentence[hi] != " ":
        space = sentence.rfind(" ", at, hi)
        hi = space if space > at else hi
    return ("\u2026" if lo > 0 else "") + sentence[lo:hi].strip() + ("\u2026" if hi < len(sentence) else "")
