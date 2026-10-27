"""BibTeX written by meertex (docs/DESIGN.md §16): the values "Take this
value" writes, and whole entries made from a record.

Values come from records as plain Unicode. They are escaped for LaTeX, page
ranges get ``--``, and title words with a capital after their first letter
are kept in braces, so styles that lowercase titles leave ``GPU`` alone.
"""

import re
import unicodedata
from datetime import date

from . import latex

_SPECIALS = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_"}
STOP_WORDS = {
    "a", "an", "the", "of", "for", "on", "in", "and", "to", "with", "towards", "toward", "via", "using", "is",
    "are", "at", "by", "from", "as", "into", "do", "does", "how", "what", "why", "when", "can", "we", "our",
}


def escape(text: str) -> str:
    """Plain text as a BibTeX value: LaTeX's specials escaped and braces
    that would not balance dropped."""
    text = re.sub(r"\s+", " ", text or "").strip()
    out = []
    depth = 0
    for ch in text:
        if ch in _SPECIALS:
            out.append(_SPECIALS[ch])
        elif ch == "{":
            depth += 1
            out.append(ch)
        elif ch == "}":
            if depth:
                depth -= 1
                out.append(ch)
        elif ch == "\\":
            out.append(r"\textbackslash{}")
        else:
            out.append(ch)
    result = "".join(out)
    while depth:
        i = result.rfind("{")
        result = result[:i] + result[i + 1 :]
        depth -= 1
    return result


def title(text: str) -> str:
    """A title as a BibTeX value, with words like GPU, BERT or iPhone in
    braces."""

    def protect(m: re.Match) -> str:
        word = m.group(0)
        return "{" + word + "}" if any(c.isupper() for c in word[1:]) else word

    return re.sub(r"[^\s{}]+", protect, escape(text))


def people(persons: list[dict]) -> str:
    """``[{family, given}]`` as a BibTeX name list. A name without a given
    name that has spaces (an organisation) is kept whole in braces."""
    names = []
    for p in persons:
        family = escape(str(p.get("family") or ""))
        given = escape(str(p.get("given") or ""))
        if not family:
            continue
        if given:
            names.append(f"{family}, {given}")
        elif " " in family:
            names.append("{" + family + "}")
        else:
            names.append(family)
    return " and ".join(names)


def pages(value: str) -> str:
    value = re.sub(r"\s+", "", value or "")
    return re.sub(r"(?<=[\dA-Za-z])[-‐-―]+(?=[\dA-Za-z])", "--", value)


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", latex.to_text(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text.lower().replace("ß", "ss"))


def make_key(record: dict, taken: set[str]) -> str:
    """``vaswani2017attention``: the first author's family name, the year and
    the first title word that is not a stop word, with b, c and on appended
    while the key is taken (compared without case)."""
    persons = record.get("people") or []
    family = latex.family_key(str(persons[0].get("family") or "")) if persons else ""
    first = _ascii(family) or "anon"
    year = str(record.get("year") or "")
    word = next((w for w in latex.words(record.get("title") or "") if w not in STOP_WORDS and _ascii(w)), "")
    base = f"{first}{year}{_ascii(word)}"
    lowered = {k.lower() for k in taken}
    key = base
    suffix = ord("b")
    while key.lower() in lowered:
        key = base + (chr(suffix) if suffix <= ord("z") else str(suffix - ord("z") + 1))
        suffix += 1
    return key


# CSL types to BibTeX types.
_TYPES = {
    "article-journal": "article",
    "journal-article": "article",
    "article": "article",
    "paper-conference": "inproceedings",
    "proceedings-article": "inproceedings",
    "chapter": "incollection",
    "book-chapter": "incollection",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "report": "techreport",
    "thesis": "phdthesis",
    "dissertation": "phdthesis",
    "dataset": "misc",
    "software": "software",
    "webpage": "online",
    "posted-content": "misc",
    "preprint": "misc",
}


def entry_type(record: dict) -> str:
    if record.get("arxiv") and record.get("source") in ("doi.org", "arxiv"):
        return "misc"
    if record.get("source") == "web" and not record.get("doi"):
        return "online"
    return _TYPES.get(str(record.get("type") or "").lower(), "misc")


def fields_from_record(record: dict, today: date) -> tuple[str, list[tuple[str, str]]]:
    """``(type, [(name, value)])`` of a new entry made from a record."""
    kind = entry_type(record)
    fields: list[tuple[str, str]] = []

    def add(name: str, value: object) -> None:
        if value:
            fields.append((name, str(value)))

    add("author", people(record.get("people") or []))
    add("title", title(record.get("title") or ""))
    venue = escape(record.get("venue") or "")
    if kind == "article":
        add("journal", venue)
    elif kind in ("inproceedings", "incollection"):
        add("booktitle", venue)
    add("year", record.get("year"))
    add("volume", escape(record.get("volume") or ""))
    add("number", escape(record.get("number") or ""))
    add("pages", pages(record.get("pages") or ""))
    if kind != "misc" or not record.get("arxiv"):
        add("publisher", escape(record.get("publisher") or ""))
    if record.get("arxiv"):
        add("eprint", record["arxiv"])
        add("archiveprefix", "arXiv")
    add("isbn", record.get("isbn"))
    add("doi", record.get("doi"))
    if kind == "online" or not record.get("doi"):
        add("url", record.get("url"))
    if kind == "online":
        add("urldate", today.isoformat())
    return kind, fields
