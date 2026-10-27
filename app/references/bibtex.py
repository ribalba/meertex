"""BibTeX files as bibtex and biber read them (docs/DESIGN.md §16, Entries).

The reference check reads every .bib file of a project, and marking an entry
as checked writes one field into it. So the parser keeps where every entry
and every field starts and ends, and the editing functions change only those
characters and leave the rest of the file as it was.

An entry that cannot be parsed becomes a problem on the line of its ``@``.
Parsing goes on at the next line that starts with ``@``, so one broken entry
does not hide the entries after it.

Positions are Python string indices (code points) into the file's text.
Lines count from 1.
"""

import datetime
import hashlib
import re
from bisect import bisect_right
from dataclasses import dataclass
from dataclasses import field as _field

MARK_FIELD = "meertex-checked"
# The fingerprint hashes this first. A new version changes every fingerprint
# and so turns every checked mark into a changed one.
FINGERPRINT_PREFIX = b"meertex-entry-1\n"

MONTHS = {
    "jan": "January",
    "feb": "February",
    "mar": "March",
    "apr": "April",
    "may": "May",
    "jun": "June",
    "jul": "July",
    "aug": "August",
    "sep": "September",
    "oct": "October",
    "nov": "November",
    "dec": "December",
}

# Entry types, field names and macro names. bibtex allows every printable
# character in them except these. The @ is left out too, so a missing closing
# brace before the next entry is noticed there.
_NAME = re.compile(r'[^\s"#%\'(),={}@]+')
# A key may hold anything but white space, commas, braces, parentheses and quotes.
_KEY = re.compile(r'[^\s,{}()"]+')
_DIGITS = re.compile(r"[0-9]+")
_WS = re.compile(r"\s*")
# Inside an entry biber also skips % comments between the parts, so they are
# skipped here as well. Inside a value a % is an ordinary character.
_SKIP = re.compile(r"(?:\s+|%[^\n]*)*")
_SPACES = re.compile(r"\s+")
_BRACES = re.compile(r"[{}]")
_QUOTED = re.compile(r'[{}"]')
_GROUP = re.compile(r"[{}()]")
_LINE_START_AT = re.compile(r"^[^\S\n]*@", re.MULTILINE)
_HEX = re.compile(r"[0-9a-fA-F]{4,64}")


@dataclass
class Field:
    name: str
    value: str
    raw: str
    start: int
    end: int
    value_start: int
    value_end: int
    line: int


@dataclass
class Entry:
    type: str
    key: str
    path: str
    start: int
    end: int
    line: int
    end_line: int
    fields: list[Field]
    inherited: dict[str, str] = _field(default_factory=dict)

    def get(self, name: str) -> str | None:
        """The value of the field, its own or else one from its crossref parent."""
        own = self.field(name)
        if own is not None:
            return own.value
        return self.inherited.get(name.lower())

    def field(self, name: str) -> Field | None:
        """The entry's own field of that name. bibtex uses the first of two."""
        name = name.lower()
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def values(self) -> dict[str, str]:
        """The entry's own fields in the order of the file."""
        out: dict[str, str] = {}
        for f in self.fields:
            out.setdefault(f.name, f.value)
        return out


@dataclass
class Problem:
    path: str
    line: int
    message: str
    key: str | None = None


@dataclass
class BibFile:
    path: str
    text: str
    entries: list[Entry]
    problems: list[Problem]


@dataclass
class Mark:
    date: str
    by: str
    hash: str


class _Broken(Exception):
    """An entry that cannot be parsed. Parsing goes on after it."""

    def __init__(self, message: str, key: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.key = key


def _cap(phrase: str) -> str:
    return phrase[:1].upper() + phrase[1:]


class _Parser:
    def __init__(self, text: str, path: str) -> None:
        self.text = text
        self.n = len(text)
        self.path = path
        self.macros = dict(MONTHS)
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line(self, pos: int) -> int:
        return bisect_right(self.line_starts, pos)

    def starts_line(self, pos: int) -> bool:
        """Whether only white space comes before ``pos`` on its line."""
        start = self.text.rfind("\n", 0, pos) + 1
        return self.text[start:pos].strip(" \t\r\f\v﻿") == ""

    def skip(self, pos: int) -> int:
        return _SKIP.match(self.text, pos).end()

    def parse(self) -> BibFile:
        entries: list[Entry] = []
        problems: list[Problem] = []
        pos = 0
        while True:
            at = self.text.find("@", pos)
            if at < 0:
                break
            try:
                entry, pos = self.item(at)
            except _Broken as broken:
                problems.append(Problem(self.path, self.line(at), broken.message, broken.key))
                again = _LINE_START_AT.search(self.text, at + 1)
                if again is None:
                    break
                pos = again.end() - 1
                continue
            if entry is not None:
                entries.append(entry)
        return BibFile(self.path, self.text, entries, problems)

    def item(self, at: int) -> tuple[Entry | None, int]:
        """What starts at the ``@`` at ``at``, and where the text after it starts."""
        text = self.text
        m = _NAME.match(text, _WS.match(text, at + 1).end())
        if m is None:
            # An @ that starts nothing, as in an email address between entries.
            return None, at + 1
        kind = m.group().lower()
        j = _WS.match(text, m.end()).end()
        if j >= self.n or text[j] not in "{(":
            # Text between entries is a comment for bibtex, also when it holds
            # an @. Only a line that starts with @type looks like a broken entry.
            if kind == "comment" or not self.starts_line(at):
                return None, m.end()
            raise _Broken(f"The @{kind} entry has no opening brace.")
        closer = "}" if text[j] == "{" else ")"

        if kind == "comment":
            end = self.group_end(j, closer)
            return None, end if end > 0 else m.end()
        if kind == "preamble":
            end = self.group_end(j, closer)
            if end < 0:
                raise _Broken(f"The @preamble never closes its {_delimiters(closer)}.")
            return None, end
        if kind == "string":
            fields, end = self.fields(j + 1, closer, "the @string", None)
            for f in fields:
                self.macros[f.name] = f.value
            return None, end

        k = self.skip(j + 1)
        km = _KEY.match(text, k)
        key = km.group() if km else ""
        after = self.skip(km.end()) if km else k
        c = text[after] if after < self.n else ""
        if not key or "=" in key or c == "=":
            raise _Broken(f"The @{kind} entry has no key.")
        owner = f"the entry {key}"
        if c == closer:
            return self.entry(kind, key, at, after + 1, []), after + 1
        if c != ",":
            if c in ("", "@"):
                raise _Broken(f"{_cap(owner)} never closes its {_delimiters(closer)}.", key)
            raise _Broken(f"{_cap(owner)} has no comma after its key.", key)
        fields, end = self.fields(after + 1, closer, owner, key)
        return self.entry(kind, key, at, end, fields), end

    def entry(self, kind: str, key: str, at: int, end: int, fields: list[Field]) -> Entry:
        return Entry(
            type=kind,
            key=key,
            path=self.path,
            start=at,
            end=end,
            line=self.line(at),
            end_line=self.line(end - 1),
            fields=fields,
        )

    def fields(self, pos: int, closer: str, owner: str, key: str | None) -> tuple[list[Field], int]:
        """The fields from ``pos`` up to the closing delimiter, and the
        position after it. ``owner`` names the entry in messages."""
        text = self.text
        never_closes = f"{_cap(owner)} never closes its {_delimiters(closer)}."
        fields: list[Field] = []
        while True:
            p = self.skip(pos)
            if p >= self.n:
                raise _Broken(never_closes, key)
            if text[p] == closer:
                return fields, p + 1
            m = _NAME.match(text, p)
            if m is None:
                if text[p] == "@":
                    raise _Broken(never_closes, key)
                raise _Broken(f"{_cap(owner)} has unexpected text where a field name should be.", key)
            name = m.group().lower()
            q = self.skip(m.end())
            if q >= self.n or text[q] != "=":
                if q >= self.n or text[q] == "@":
                    raise _Broken(never_closes, key)
                raise _Broken(f"The field {name} in {owner} has no = sign.", key)
            value_start = self.skip(q + 1)
            value, value_end = self.value(value_start, name, owner, key, never_closes)
            fields.append(
                Field(
                    name=name,
                    value=value,
                    raw=text[value_start:value_end],
                    start=m.start(),
                    end=value_end,
                    value_start=value_start,
                    value_end=value_end,
                    line=self.line(m.start()),
                )
            )
            r = self.skip(value_end)
            c = text[r] if r < self.n else ""
            if c == ",":
                pos = r + 1
                continue
            if c == closer:
                return fields, r + 1
            if c in ("", "@"):
                raise _Broken(never_closes, key)
            following = _NAME.match(text, r)
            if following and text.startswith("=", self.skip(following.end())):
                raise _Broken(f"{_cap(owner)} has no comma before the field {following.group().lower()}.", key)
            raise _Broken(f"{_cap(owner)} has unexpected text after the field {name}.", key)

    def value(self, pos: int, name: str, owner: str, key: str | None, never_closes: str) -> tuple[str, int]:
        """A value with its ``#`` parts joined and its macros expanded, and
        the position just after its last part."""
        text = self.text
        parts: list[str] = []
        p = pos
        while True:
            c = text[p] if p < self.n else ""
            if c == "{":
                end = self.brace_end(p)
                if end < 0:
                    raise _Broken(never_closes, key)
                parts.append(text[p + 1 : end - 1])
            elif c == '"':
                end = self.quote_end(p, name, owner, key)
                parts.append(text[p + 1 : end - 1])
            else:
                m = _NAME.match(text, p)
                if m is None:
                    if c in ("", "@"):
                        raise _Broken(never_closes, key)
                    if parts:
                        raise _Broken(f"The field {name} in {owner} has nothing after its #.", key)
                    raise _Broken(f"The field {name} in {owner} has no value.", key)
                token = m.group()
                end = m.end()
                if _DIGITS.fullmatch(token):
                    parts.append(token)
                else:
                    # An unknown macro stays as it is written. bibtex warns, but
                    # the entry still works.
                    parts.append(self.macros.get(token.lower(), token))
            q = self.skip(end)
            if q < self.n and text[q] == "#":
                p = self.skip(q + 1)
                continue
            return "".join(parts), end

    def brace_end(self, pos: int) -> int:
        """The position after the brace that closes the one at ``pos``, or -1.
        Every brace counts, also an escaped one, as in bibtex."""
        depth = 0
        for m in _BRACES.finditer(self.text, pos):
            depth += 1 if m.group() == "{" else -1
            if depth == 0:
                return m.end()
        return -1

    def quote_end(self, pos: int, name: str, owner: str, key: str | None) -> int:
        """The position after the quote that ends the quoted value at ``pos``.
        A quote inside braces is an ordinary character."""
        depth = 0
        for m in _QUOTED.finditer(self.text, pos + 1):
            c = m.group()
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    raise _Broken(f"The value of {name} in {owner} has a closing brace without an opening one.", key)
                depth -= 1
            elif depth == 0:
                return m.end()
        raise _Broken(f"The value of {name} in {owner} never closes its quotes.", key)

    def group_end(self, pos: int, closer: str) -> int:
        """The end of a @comment or @preamble group, or -1."""
        if closer == "}":
            return self.brace_end(pos)
        depth = 0
        for m in _GROUP.finditer(self.text, pos + 1):
            c = m.group()
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == ")" and depth <= 0:
                return m.end()
        return -1


def _delimiters(closer: str) -> str:
    return "braces" if closer == "}" else "parentheses"


def parse(text: str, path: str = "") -> BibFile:
    """The entries and the problems of a .bib file, in the order of the file."""
    return _Parser(text, path).parse()


def resolve_crossrefs(entries: list[Entry]) -> None:
    """Fill ``inherited`` of every entry with a ``crossref``, from the fields
    its parent has and it does not. The entries may come from several files."""
    by_key: dict[str, Entry] = {}
    by_folded: dict[str, Entry] = {}
    for e in entries:
        by_key.setdefault(e.key, e)
        by_folded.setdefault(e.key.casefold(), e)
    for e in entries:
        ref = e.field("crossref")
        if ref is None:
            continue
        target = ref.value.strip()
        parent = by_key.get(target) or by_folded.get(target.casefold())
        if parent is None or parent is e:
            continue
        own = {f.name for f in e.fields}
        inherited: dict[str, str] = {}
        for f in parent.fields:
            if f.name in own or f.name in ("crossref", "ids", MARK_FIELD):
                continue
            inherited.setdefault(f.name, f.value)
        e.inherited = inherited


def fingerprint(entry: Entry) -> str:
    """The SHA-256 of the entry's type and its own fields, without its key and
    without the checked mark (DESIGN §16, Entries)."""
    lines = sorted((f.name, _SPACES.sub(" ", f.value).strip()) for f in entry.fields if f.name != MARK_FIELD)
    canonical = entry.type.lower() + "\n" + "".join(f"{name}={value}\n" for name, value in lines)
    return hashlib.sha256(FINGERPRINT_PREFIX + canonical.encode("utf-8")).hexdigest()


def parse_mark(value: str) -> Mark | None:
    """The day, the person and the hash of a meertex-checked value, or None
    when it is not one."""
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3 or not _HEX.fullmatch(parts[2]):
        return None
    return Mark(date=parts[0], by=_SPACES.sub(" ", parts[1]), hash=parts[2].lower())


def mark_value(day: datetime.date, by: str, fingerprint: str) -> str:
    """The value of a meertex-checked field."""
    if isinstance(day, datetime.datetime):
        day = day.date()
    name = _SPACES.sub(" ", re.sub(r"[,{}]", "", by)).strip()
    return f"{day.isoformat()}, {name}, {fingerprint[:8]}"


def braces_balanced(value: str) -> bool:
    """Whether every closing brace closes an earlier opening one and none
    stays open."""
    depth = 0
    for m in _BRACES.finditer(value):
        depth += 1 if m.group() == "{" else -1
        if depth < 0:
            return False
    return depth == 0


def _check(text: str, entry: Entry) -> None:
    if not (0 <= entry.start < entry.end <= len(text)) or text[entry.start] != "@" or text[entry.end - 1] not in "})":
        raise ValueError("The entry does not belong to this text.")


def _newline(text: str, entry: Entry) -> str:
    own = text[entry.start : entry.end]
    if "\r\n" in own or ("\n" not in own and "\r\n" in text):
        return "\r\n"
    return "\n"


def _key_end(text: str, entry: Entry) -> int:
    """The position just after the entry's key."""
    j = _WS.match(text, entry.start + 1).end()
    j = _NAME.match(text, j).end()
    j = _WS.match(text, j).end() + 1
    j = _SKIP.match(text, j).end()
    if not text.startswith(entry.key, j):
        raise ValueError("The entry does not belong to this text.")
    return j + len(entry.key)


def _comma_at(text: str, pos: int, limit: int) -> int:
    """The position of the comma that follows ``pos``, or -1."""
    p = _SKIP.match(text, pos).end()
    return p if p < limit and text[p] == "," else -1


def _indent(text: str, pos: int, entry: Entry) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    if line_start <= entry.start:
        return "  "
    return re.match(r"[ \t]*", text[line_start:pos]).group()


def set_field(text: str, entry: Entry, name: str, value: str) -> str:
    """The file's text with the field set to ``{value}``. An existing field
    keeps its place, and only its value changes. A new field becomes the
    entry's last field, on a line of its own. ``entry`` comes from
    ``parse(text)``."""
    if not braces_balanced(value):
        raise ValueError("The value has unbalanced braces.")
    _check(text, entry)
    existing = entry.field(name)
    if existing is not None:
        return text[: existing.value_start] + "{" + value + "}" + text[existing.value_end :]

    nl = _newline(text, entry)
    close = entry.end - 1
    if entry.fields:
        last = entry.fields[-1]
        after = last.end
        indent = _indent(text, last.start, entry)
    else:
        after = _key_end(text, entry)
        indent = "  "
    comma = "" if _comma_at(text, after, close) >= 0 else ","
    line = f"{indent}{name} = {{{value}}},"

    close_line = text.rfind("\n", 0, close) + 1
    if close_line > after and text[close_line:close].strip() == "":
        # The closing delimiter has a line of its own. The field goes on a
        # new line just above it.
        return text[:after] + comma + text[after:close_line] + line + nl + text[close_line:]
    # The delimiter shares a line with the last field, or the whole entry is
    # on one line. The field and the delimiter go on lines of their own.
    cut = close
    while cut > after and text[cut - 1] in " \t":
        cut -= 1
    return text[:after] + comma + text[after:cut] + nl + line + nl + text[close:]


def remove_field(text: str, entry: Entry, name: str) -> str:
    """The file's text without the field. A field on lines of its own takes
    those lines with it. ``entry`` comes from ``parse(text)``."""
    f = entry.field(name)
    if f is None:
        return text
    _check(text, entry)
    comma = _comma_at(text, f.end, entry.end - 1)
    end = comma + 1 if comma >= 0 else f.end
    line_start = text.rfind("\n", 0, f.start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    if line_start > entry.start and not text[line_start : f.start].strip() and not text[end:line_end].strip():
        return text[:line_start] + text[min(line_end + 1, len(text)) :]

    start = f.start
    rest = end
    while rest < len(text) and text[rest] in " \t":
        rest += 1
    if rest > end and rest < len(text) and text[rest] not in "\r\n":
        end = rest
    else:
        while start > line_start and text[start - 1] in " \t":
            start -= 1
    return text[:start] + text[end:]


def format_entry(type: str, key: str, fields: list[tuple[str, str]]) -> str:
    """A new entry in the form meertex writes, with a newline at the end."""
    if not _NAME.fullmatch(type or ""):
        raise ValueError(f"{type!r} is not an entry type.")
    if not _KEY.fullmatch(key or "") or "=" in key:
        raise ValueError(f"{key!r} is not a key bibtex can read.")
    lines = [f"@{type.lower()}{{{key},"]
    for name, value in fields:
        if not _NAME.fullmatch(name or ""):
            raise ValueError(f"{name!r} is not a field name.")
        if not braces_balanced(value):
            raise ValueError(f"The value of {name.lower()} has unbalanced braces.")
        lines.append(f"  {name.lower()} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines) + "\n"
