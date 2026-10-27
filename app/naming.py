"""Project names from the owner's naming pattern (DESIGN §4).

The name is the checkout folder's name, so it must be safe as a path segment
and as a git-friendly word, and it never follows later edits of the fields.
It is built once, here, when a project is created or forked.
"""

import re
import unicodedata

from sqlalchemy.orm import Session as DBSession

from .models import Project

PLACEHOLDER = re.compile(r"\{(year|conference|paper|track|title)(\|(lower|upper))?\}")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
NAME_MAX = 120

STOP_WORDS = frozenset({"a", "an", "the", "of", "for", "on", "in", "and", "to", "with", "towards", "via", "using"})

# German spells these out instead of dropping the dots. Everything else loses
# its accent through NFKD below.
_SPELLED_OUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS"})
# Marks where a placeholder came out empty, so it can collapse with its separator.
_EMPTY = "\0"
_SEPARATORS = "-_."
_COLLAPSE = re.compile(r"[-_.]*(?:\0[-_.]*)+")
_LITERAL_OK = re.compile(r"^[A-Za-z0-9._-]*$")


def slug(value: object) -> str:
    """``Grüße aus Köln!`` becomes ``Gruesse-aus-Koeln``. Case is kept."""
    text = unicodedata.normalize("NFC", str(value)).translate(_SPELLED_OUT)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def derive_paper(title: str) -> str:
    """The paper's short name, derived from its title. It is the first three
    words that are not stop words, slugged and lowercased."""
    words = []
    for word in (title or "").split():
        s = slug(word).lower()
        if s and s not in STOP_WORDS:
            words.append(s)
        if len(words) == 3:
            break
    return "-".join(words)


def _value(field: str, fields: dict) -> str:
    raw = fields.get(field)
    if field == "paper" and not raw:
        return derive_paper(fields.get("title") or "")
    return slug(raw) if raw not in (None, "") else ""


def _collapse(match: re.Match) -> str:
    # At either end the separator goes with the empty placeholder. In the
    # middle one separator stays, the one written before the placeholder.
    if match.start() == 0 or match.end() == len(match.string):
        return ""
    pieces = [p for p in match.group(0).split(_EMPTY) if p]
    return pieces[0] if pieces else ""


def render(pattern: str, fields: dict) -> str:
    """The name ``pattern`` gives for ``fields`` (year, conference, paper,
    track, title), before collisions are handled. Can be empty."""

    def replace(m: re.Match) -> str:
        value = _value(m.group(1), fields)
        if m.group(3) == "lower":
            value = value.lower()
        elif m.group(3) == "upper":
            value = value.upper()
        return value or _EMPTY

    name = _COLLAPSE.sub(_collapse, PLACEHOLDER.sub(replace, pattern)).replace(_EMPTY, "")
    return trim(name)


def trim(name: str, limit: int = NAME_MAX) -> str:
    """Cut to ``limit`` characters without leaving a separator at either end."""
    name = name.lstrip(_SEPARATORS)
    return name[:limit].rstrip(_SEPARATORS)


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def validate_pattern(pattern: str) -> str | None:
    """None when the pattern is usable, else the reason it is not."""
    if not PLACEHOLDER.search(pattern):
        return "The naming pattern needs at least one placeholder, e.g. {year}-{conference|lower}-{paper|lower}"
    literal = PLACEHOLDER.sub("", pattern)
    if "{" in literal or "}" in literal:
        return "The naming pattern knows only {year}, {conference}, {paper}, {track} and {title}, each with an optional |lower or |upper"
    if not _LITERAL_OK.match(literal):
        return "Text outside the placeholders may only use letters, digits, '.', '_' and '-'"
    return None


def unique_name(db: DBSession, owner_id: str, base: str, exclude_id: str | None = None) -> str:
    """``base``, or ``base-2``, ``base-3`` and so on, whichever the owner does not
    use yet. ``exclude_id`` is a project whose own name does not count."""
    query = db.query(Project.name).filter(Project.owner_id == owner_id)
    if exclude_id:
        query = query.filter(Project.id != exclude_id)
    taken = {n.lower() for (n,) in query.all()}
    if base.lower() not in taken:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = trim(base, NAME_MAX - len(suffix)) + suffix
        if candidate.lower() not in taken:
            return candidate
        n += 1
