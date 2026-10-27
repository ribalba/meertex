"""What the reference check finds about one entry (docs/DESIGN.md §16).

``offline`` needs nothing but the entry and is run on every read. ``lookup``
asks the sources and is run by a check run, which keeps its result per
fingerprint. Both return findings as dicts in the shape of §16:

    {code, level, field, message, value, record_value, source, url}

The comparison is lenient on purpose. A checker that complains about correct
entries is not read any more, so anything that can be a style difference
(case, accents, LaTeX, abbreviations, "and others") does not count, and only
what a person must look at is an error.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from . import build, latex
from .bibtex import MARK_FIELD, Entry
from .sources import Answer, Sources, normalize_doi

ERROR, WARNING, INFO = "error", "warning", "info"

# Types whose works are in the indexes. Anything else may be grey literature.
INDEXED_TYPES = {"article", "inproceedings", "incollection", "conference"}
WEB_TYPES = {"online", "misc", "software", "electronic", "www"}
BOOK_TYPES = {"book", "inbook", "booklet", "mvbook", "collection", "proceedings"}
PREPRINT_TYPES = {"article", "misc", "online", "unpublished", "techreport", "report", "preprint"}
NO_PLACEHOLDER_CHECK = {"url", "urldate", "file", "abstract", "keywords", "annote", "eprint", "doi", "isbn", "issn",
                        "crossref", MARK_FIELD}

PLACEHOLDERS = [
    re.compile(r"\bX{3,}\b"),
    re.compile(r"\bx{4,}\b"),
    re.compile(r"\?\?"),
    re.compile(r"\b(?:TODO|TBD|FIXME)\b"),
    re.compile(r"\bauthor\s+name\b", re.IGNORECASE),
    re.compile(r"\b(?:last|first)\s*name\b", re.IGNORECASE),
    re.compile(r"\[citation needed\]", re.IGNORECASE),
]
NOT_FOUND_PAGE = re.compile(
    r"\b(?:404|page not found|not found|no longer available|does(?: not|n't) exist|nicht gefunden|"
    r"seite existiert nicht|page introuvable)\b",
    re.IGNORECASE,
)
RETRACTIONS = {"retraction", "withdrawal", "removal", "partial_retraction"}
CORRECTIONS = {"correction", "erratum", "corrigendum", "addendum", "expression_of_concern", "clarification"}
VENUE_STOP = {
    "proceedings", "proc", "of", "the", "on", "in", "and", "for", "international", "intl", "conference", "conf",
    "annual", "symposium", "symp", "workshop", "journal", "j", "transactions", "trans", "acm", "ieee", "a", "an",
    "to", "with", "at", "th", "st", "nd", "rd", "companion", "volume", "vol", "part",
}
TRIVIAL = {"of", "on", "the", "and", "for", "in", "a", "an", "to", "at", "with"}
# The fields a record may add to an entry of a type, as "missing".
MISSING_BY_TYPE = {
    "article": ("pages", "volume", "number"),
    "inproceedings": ("pages",),
    "conference": ("pages",),
    "incollection": ("pages", "publisher"),
    "inbook": ("pages", "publisher"),
    "book": ("publisher",),
    "proceedings": ("publisher",),
    "techreport": (),
    "misc": (),
    "online": (),
}

ARXIV_NEW = re.compile(r"(?<![\w.])(\d{4})\.(\d{4,5})(?:v\d+)?(?![\w.])")
ARXIV_OLD = re.compile(r"(?<![\w.-])([a-z][a-z-]*(?:\.[A-Z]{2})?)/(\d{7})(?:v\d+)?(?!\w)")


def finding(
    code: str,
    level: str,
    message: str,
    *,
    field: str | None = None,
    value: str | None = None,
    record_value: str | None = None,
    source: str = "bibtex",
    url: str | None = None,
    **extra: object,
) -> dict:
    return {"code": code, "level": level, "field": field, "message": message, "value": value,
            "record_value": record_value, "source": source, "url": url, **extra}


# --- Identifiers ------------------------------------------------------------------


@dataclass
class Ids:
    doi: str | None = None          # from the doi field or a doi.org link
    doi_raw: str | None = None      # the doi field as written
    arxiv: str | None = None
    isbn: str | None = None
    urls: list[str] = field(default_factory=list)

    @property
    def lookup_doi(self) -> str | None:
        return self.doi or (f"10.48550/arXiv.{self.arxiv}" if self.arxiv else None)


def arxiv_in(text: str) -> str | None:
    m = ARXIV_NEW.search(text or "")
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = ARXIV_OLD.search(text or "")
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


def _urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s{}\\]+", text or "")


def identifiers(entry: Entry) -> Ids:
    ids = Ids()
    raw = (entry.get("doi") or "").strip()
    if raw:
        ids.doi_raw = raw
        ids.doi = normalize_doi(latex.to_text(raw).replace(" ", ""))
    urls = _urls(entry.get("url") or "")
    m = re.search(r"\\(?:url|href)\{([^}]+)\}", entry.get("howpublished") or "")
    if m:
        urls += _urls(m.group(1))
    for u in urls:
        host = (urlsplit(u).hostname or "").lower()
        if host in ("doi.org", "dx.doi.org"):
            ids.doi = ids.doi or normalize_doi(u)
        elif host.endswith("arxiv.org") and re.search(r"/(?:abs|pdf|html)/", u):
            ids.arxiv = ids.arxiv or arxiv_in(u.split("/", 3)[-1])
        else:
            ids.urls.append(u.rstrip(".,;"))
    kind = (entry.get("eprinttype") or entry.get("archiveprefix") or "").strip().lower()
    eprint = entry.get("eprint") or ""
    if eprint and (kind == "arxiv" or (not kind and arxiv_in(eprint))):
        ids.arxiv = ids.arxiv or arxiv_in(eprint)
    for name in ("journal", "journaltitle", "howpublished", "note", "volume", "number"):
        text = entry.get(name) or ""
        if re.search(r"arxiv|corr|abs/", text, re.IGNORECASE):
            ids.arxiv = ids.arxiv or arxiv_in(text)
    if ids.doi and ids.doi.startswith("10.48550/arxiv."):
        ids.arxiv = ids.arxiv or ids.doi.split("arxiv.", 1)[1]
    isbn = re.search(r"[\dXx][\dXx\s-]{8,}[\dXx]", entry.get("isbn") or "")
    if isbn:
        ids.isbn = re.sub(r"[\s-]", "", isbn.group(0)).upper()
    return ids


# --- Without the network ----------------------------------------------------------


def entry_year(entry: Entry) -> str:
    year = (entry.get("year") or "").strip()
    if not year:
        m = re.match(r"\s*(\d{4})", entry.get("date") or "")
        year = m.group(1) if m else ""
    return latex.to_text(year)


def isbn_ok(isbn: str) -> bool:
    if re.fullmatch(r"\d{9}[\dX]", isbn):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(isbn))
        return total % 11 == 0
    if re.fullmatch(r"\d{13}", isbn):
        return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn)) % 10 == 0
    return False


def _arxiv_problem(arxiv: str, today: date) -> str | None:
    """Why an arXiv id cannot exist, or None."""
    m = re.fullmatch(r"(\d{2})(\d{2})\.(\d{4,5})", arxiv)
    if m:
        yy, mm, number = int(m.group(1)), int(m.group(2)), m.group(3)
        if not 1 <= mm <= 12 or (yy, mm) < (7, 4):
            return f"{arxiv} is not an arXiv id. New ids start in April 2007 and their first four digits are year and month."
        if ((yy, mm) >= (15, 1)) != (len(number) == 5):
            return f"{arxiv} is not an arXiv id. Since 2015 the number after the dot has five digits, before that four."
        if (2000 + yy, mm) > (today.year, today.month):
            return f"The arXiv id {arxiv} is from {2000 + yy}-{mm:02d}, which has not come yet."
        return None
    m = re.fullmatch(r"[a-z][a-z-]*(?:\.[A-Z]{2})?/(\d{2})(\d{2})\d{3}", arxiv)
    if m:
        yy, mm = int(m.group(1)), int(m.group(2))
        if not 1 <= mm <= 12 or not (yy >= 91 or yy <= 7):
            return f"{arxiv} is not an arXiv id. Old ids have the year and month after the slash, from 1991 to 2007."
        return None
    return f"{arxiv} is not an arXiv id."


def arxiv_year(arxiv: str) -> int | None:
    m = re.fullmatch(r"(\d{2})\d{2}\.\d{4,5}", arxiv)
    if m:
        return 2000 + int(m.group(1))
    m = re.fullmatch(r".+/(\d{2})\d{5}", arxiv)
    if m:
        yy = int(m.group(1))
        return (1900 if yy >= 91 else 2000) + yy
    return None


def is_preprint(entry: Entry) -> bool:
    """An entry that cites the arXiv version, not a published one."""
    if entry.type not in PREPRINT_TYPES or entry.get("booktitle"):
        return False
    venue = latex.fold(entry.get("journal") or entry.get("journaltitle") or "")
    return not venue or bool(re.search(r"\b(?:arxiv|corr|preprint)\b", venue))


def offline(entry: Entry, project_year: int | None, today: date) -> list[dict]:
    """The findings that need no network, for one entry."""
    out: list[dict] = []
    ids = identifiers(entry)

    for f in entry.fields:
        if f.name in NO_PLACEHOLDER_CHECK:
            continue
        text = latex.to_text(f.value)
        bad = text.strip() in ("...", "…") or any(p.search(text) for p in PLACEHOLDERS)
        if not bad and f.name in ("author", "editor") and re.search(r"\bet\.?\s*al\b", text, re.IGNORECASE):
            bad = True
        if bad:
            out.append(finding("placeholder", ERROR, f"The {f.name} is a placeholder: {text.strip()[:120]}",
                               field=f.name, value=f.value))

    year = entry_year(entry)
    field_name = "year" if entry.get("year") else "date"
    if year:
        if not re.fullmatch(r"\d{4}", year) or int(year) < 1450:
            out.append(finding("year", WARNING, f"{year} is not a year.", field=field_name, value=year))
        elif int(year) > today.year:
            out.append(finding("year", ERROR, f"The year {year} has not come yet.", field=field_name, value=year))
        elif project_year and int(year) > project_year:
            out.append(finding("year", WARNING, f"The year {year} is after this paper's own year, {project_year}.",
                               field=field_name, value=year))

    m = re.fullmatch(r"\s*(\d+)\s*(?:-+|\u2013|\u2014)\s*(\d+)\s*", latex.to_text(entry.get("pages") or ""))
    if m and int(m.group(2)) < int(m.group(1)):
        out.append(finding("pages", WARNING, f"The last page, {m.group(2)}, comes before the first, {m.group(1)}.",
                           field="pages", value=entry.get("pages")))

    if ids.arxiv:
        problem = _arxiv_problem(ids.arxiv, today)
        if problem:
            out.append(finding("arxiv_id", ERROR, problem, field="eprint" if entry.get("eprint") else None,
                               value=ids.arxiv))
        elif is_preprint(entry) and re.fullmatch(r"\d{4}", year or ""):
            expected = arxiv_year(ids.arxiv)
            if expected and expected != int(year):
                out.append(finding("arxiv_year", WARNING,
                                   f"The arXiv id {ids.arxiv} is from {expected}, but the entry says {year}.",
                                   field=field_name, value=year, record_value=str(expected)))

    if entry.get("isbn") and (not ids.isbn or not isbn_ok(ids.isbn)):
        out.append(finding("isbn", ERROR, f"{latex.to_text(entry.get('isbn') or '')} is not a valid ISBN. "
                           "Its check digit is wrong.", field="isbn", value=entry.get("isbn")))

    if ids.doi_raw and not ids.doi:
        out.append(finding("doi_syntax", ERROR, f"{ids.doi_raw} is not a DOI. A DOI looks like 10.1145/3290605.3300857.",
                           field="doi", value=ids.doi_raw))

    if entry.type in ("online", "www", "electronic"):
        urldate = (entry.get("urldate") or "").strip()
        if not urldate:
            out.append(finding("urldate", INFO, "A web page should say when it was visited (urldate).", field="urldate"))
        else:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", urldate)
            if m and date(int(m.group(1)), 1, 1) and (int(m.group(1)), int(m.group(2)), int(m.group(3))) > (
                today.year, today.month, today.day
            ):
                out.append(finding("urldate", WARNING, f"The visit date {urldate} has not come yet.",
                                   field="urldate", value=urldate))
    return out


# --- Comparing with a record ------------------------------------------------------


def families(entry: Entry) -> tuple[list[str], bool]:
    """The family-name keys of the entry's authors (or editors), and
    whether the list ends in "and others"."""
    names, others = latex.split_names(entry.get("author") or entry.get("editor") or "")
    keys = [latex.family_key(n) for n in names]
    return [k for k in keys if k], others


def record_families(record: dict) -> list[str]:
    keys = [latex.family_key(str(p.get("family") or "")) for p in record.get("people") or []]
    return [k for k in keys if k]


def clean_title(title: str) -> str:
    return re.sub(r"^\s*(?:retracted|withdrawn)(?:\s+article)?\s*[:.\-]\s*", "", title or "", flags=re.IGNORECASE)


def _short_people(record: dict, n: int = 3) -> str:
    names = record.get("authors") or []
    text = ", ".join(names[:n])
    return text + (" and others" if len(names) > n else "")


def _describe(record: dict) -> str:
    parts = [f"“{clean_title(record.get('title') or '')}”"]
    if record.get("authors"):
        parts.append(f"by {_short_people(record)}")
    if record.get("year"):
        parts.append(f"({record['year']})")
    return " ".join(parts)


def _venue_words(text: str) -> set[str]:
    return {w for w in latex.words(text) if w not in VENUE_STOP and not re.fullmatch(r"\d+(?:st|nd|rd|th)?", w)}


def venue_matches(entry_venue: str, record_venue: str) -> bool:
    """Whether two venue names can be the same venue: a shared word, a word
    that abbreviates another (Softw. for Software), or an acronym."""
    a, b = _venue_words(entry_venue), _venue_words(record_venue)
    if not a or not b:
        return True
    if a & b:
        return True
    for x in a:
        for y in b:
            if min(len(x), len(y)) >= 3 and (x.startswith(y) or y.startswith(x)):
                return True
    initials_b = "".join(w[0] for w in latex.words(record_venue) if w not in TRIVIAL and not w.isdigit())
    initials_a = "".join(w[0] for w in latex.words(entry_venue) if w not in TRIVIAL and not w.isdigit())
    aliases = {"neurips": "nips", "nips": "neurips"}
    for words, initials, other in ((a, initials_b, b), (b, initials_a, a)):
        for word in words | {aliases.get(w, w) for w in words}:
            if len(word) >= 2 and (word in other or _acronym_of(word, initials)):
                return True
    return False


def _acronym_of(word: str, initials: str) -> bool:
    """Whether ``word`` can be an acronym of the words whose first letters
    are ``initials``. Acronyms skip words (NAACL leaves out "chapter"), so
    its letters only have to come in order."""
    if len(word) < 3 or not word.isalpha():
        return word in initials
    it = iter(initials)
    return all(c in it for c in word)


def _pages_key(value: str) -> str:
    value = latex.to_text(value or "").lower()
    value = re.sub(r"^pp?\.\s*", "", value.strip())
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[-\u2010-\u2015]+", "-", value)
    return re.sub(r"(?:^|(?<=-))\d+:", "", value)


def compare(entry: Entry, record: dict, *, from_doi: bool) -> tuple[list[dict], bool]:
    """The findings of an entry against a record, and whether the record is
    another work altogether (only said of the record of the entry's DOI)."""
    out: list[dict] = []
    source = record.get("source") or ""
    bt = entry.get("title") or ""
    rt = clean_title(record.get("title") or "")
    sim = latex.similarity(bt, rt) if bt and rt else 1.0
    same_title = sim >= 0.97
    prefix = bool(bt and rt) and latex.is_prefix_title(bt, rt)
    mine, others = families(entry)
    theirs = record_families(record)
    common = set(mine) & set(theirs)

    if from_doi and bt and rt and sim < 0.6 and not prefix and (not mine or not theirs or not common):
        out.append(finding("doi_mismatch", ERROR, f"The DOI belongs to another work, {_describe(record)}.",
                           field="doi", value=entry.get("doi"), source=source, url=_doi_url(record)))
        return out, True

    if bt and rt and not same_title:
        how = "differs a little from" if prefix or sim >= 0.85 else "is not"
        out.append(finding("title", WARNING, f"The title {how} the record's, “{rt}”.", field="title", value=bt,
                           record_value=build.title(rt), source=source, url=_doi_url(record)))

    if mine and theirs:
        record_value = build.people(record.get("people") or [])
        if not common:
            out.append(finding("authors", ERROR, f"None of the authors is in the record, which has {_short_people(record, 5)}.",
                               field="author", value=entry.get("author"), record_value=record_value, source=source))
        else:
            words = []
            missing = [] if others else [n for n, k in zip(record.get("authors") or [], record_families(record), strict=False)
                                         if k not in set(mine)]
            extra = [k for k in mine if k not in set(theirs)]
            if missing:
                words.append(f"The record also has {_list(missing)}.")
            if extra:
                words.append(f"The record does not have {_list([k.title() for k in extra])}.")
            if not missing and not extra:
                mine_order = [k for k in dict.fromkeys(mine) if k in common]
                theirs_order = [k for k in dict.fromkeys(theirs) if k in common]
                if mine_order != theirs_order[: len(mine_order)]:
                    words.append("The authors are in another order than in the record.")
            if words:
                out.append(finding("authors", WARNING, " ".join(words), field="author", value=entry.get("author"),
                                   record_value=record_value, source=source))

    year = entry_year(entry)
    years = [y for y in record.get("years") or [] if isinstance(y, int)]
    if re.fullmatch(r"\d{4}", year) and years and int(year) not in years and not (is_preprint(entry) is False and record.get("arxiv")):
        out.append(finding("year", WARNING, f"The record has the year {record.get('year') or years[0]}, not {year}.",
                           field="year" if entry.get("year") else "date", value=year,
                           record_value=str(record.get("year") or years[0]), source=source))

    venue_field = next((n for n in ("journal", "journaltitle", "booktitle") if entry.get(n)), None)
    record_venue = record.get("venue") or ""
    if venue_field and record_venue and not record.get("arxiv") and not venue_matches(entry.get(venue_field) or "", record_venue):
        out.append(finding("venue", WARNING, f"The record's venue is “{record_venue}”.", field=venue_field,
                           value=entry.get(venue_field), record_value=build.escape(record_venue), source=source))

    for name, key in (("volume", "volume"), ("number", "number"), ("pages", "pages")):
        mine_value, theirs_value = entry.get(name) or "", str(record.get(key) or "")
        if not mine_value or not theirs_value:
            continue
        if name == "pages":
            a, b = _pages_key(mine_value), _pages_key(theirs_value)
            if a == b or b.split("-")[0] == a:
                continue
            shown = build.pages(theirs_value)
        else:
            if latex.fold(mine_value) == latex.fold(theirs_value):
                continue
            shown = build.escape(theirs_value)
        out.append(finding("differs", WARNING, f"The record has {name} {theirs_value}, not {latex.to_text(mine_value)}.",
                           field=name, value=mine_value, record_value=shown, source=source))

    if from_doi:
        # Only what the usual styles print for the type, so the list stays short.
        wanted = MISSING_BY_TYPE.get(entry.type, ("pages",))
        for name, key, shown in (
            ("pages", "pages", build.pages(str(record.get("pages") or ""))),
            ("volume", "volume", build.escape(str(record.get("volume") or ""))),
            ("number", "number", build.escape(str(record.get("number") or ""))),
            ("publisher", "publisher", build.escape(str(record.get("publisher") or ""))),
        ):
            if name not in wanted:
                continue
            if record.get(key) and not entry.get(name) and not (name == "publisher" and record.get("arxiv")):
                out.append(finding("missing", INFO, f"The record has the {name}, {record[key]}.", field=name,
                                   record_value=shown, source=source))
    return out, False


def _list(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _doi_url(record: dict) -> str | None:
    return f"https://doi.org/{record['doi']}" if record.get("doi") else (record.get("url") or None)


# --- Lookups ----------------------------------------------------------------------


@dataclass
class Lookup:
    """What a run found about an entry: the record that matched (a page
    for a working link), the findings, and whether anything answered."""

    record: dict | None = None
    findings: list[dict] = field(default_factory=list)


SOURCE_NAMES = {
    "crossref": "Crossref",
    "semantic scholar": "Semantic Scholar",
    "dblp": "DBLP",
    "arxiv": "arXiv",
    "openlibrary": "Open Library",
}


def lookup(entry: Entry, src: Sources) -> Lookup:
    """Look the entry up (§16, steps 1 to 4, then its links)."""
    ids = identifiers(entry)
    out = Lookup()
    if ids.lookup_doi:
        _by_doi(entry, ids, src, out)
    elif ids.isbn:
        _by_isbn(entry, ids, src, out)
    elif entry.get("title") and not (entry.type in WEB_TYPES and ids.urls):
        _search(entry, src, out)
    if ids.urls and (not ids.lookup_doi or entry.type in WEB_TYPES):
        _links(entry, ids, src, out)
    return out


def _by_doi(entry: Entry, ids: Ids, src: Sources, out: Lookup) -> None:
    doi = ids.lookup_doi or ""
    answer = src.doi(doi)
    if not answer.ok:
        out.findings.append(finding("unavailable", INFO, f"{answer.error} The DOI could not be checked.",
                                    field="doi", source="doi.org"))
        _search(entry, src, out, quiet=True)
        return
    record = answer.record
    if record is None and answer.info.get("no_csl"):
        # Known to doi.org, but its agency sends no metadata to compare.
        out.record = {"source": "doi.org", "doi": doi, "url": f"https://doi.org/{doi}", "title": "", "authors": [],
                      "people": [], "years": [], "year": None, "venue": "", "type": "", "volume": "", "number": "",
                      "pages": "", "publisher": "", "abstract": "", "arxiv": "", "isbn": ""}
        return
    if record is None and ids.doi:
        out.findings.append(_doi_not_found(entry, ids.doi, src))
        _search(entry, src, out, quiet=True, suggest_reason="The work with this title has the DOI {doi}.")
        return
    if record is None:
        # An arXiv id DataCite does not know. New papers can take a day.
        paper = src.arxiv_paper(ids.arxiv or "")
        if paper.ok and paper.record is None:
            out.findings.append(finding("not_found", ERROR, f"arXiv has no paper {ids.arxiv}.", field="eprint",
                                        value=ids.arxiv, source="arxiv"))
            return
        if not paper.record:
            out.findings.append(finding("unavailable", INFO, f"{paper.error} The arXiv id could not be checked.",
                                        source="arxiv"))
            return
        record = paper.record
    findings, mismatch = compare(entry, record, from_doi=bool(ids.doi) or bool(ids.arxiv))
    out.findings += findings
    if mismatch:
        _search(entry, src, out, quiet=True, suggest_reason="The work with this title has the DOI {doi}.")
        return
    out.record = record
    if record.get("arxiv") or doi.startswith("10.48550/"):
        _published_version(entry, record, src, out)
    else:
        _crossref_extras(entry, doi, src, out)


def _doi_not_found(entry: Entry, doi: str, src: Sources) -> dict:
    prefix = doi.split("/", 1)[0]
    message = f"doi.org does not know the DOI {doi}."
    owner = src.crossref_prefix(prefix)
    name = owner.info.get("name") if owner.ok else ""
    if name:
        message += f" The prefix {prefix} belongs to {name}."
        named = latex.fold(" ".join(entry.get(n) or "" for n in ("publisher", "journal", "booktitle", "organization")))
        known = [p for p in ("acm", "ieee", "springer", "elsevier", "wiley", "usenix", "arxiv", "aaai", "mdpi")
                 if re.search(rf"\b{p}\b", named)]
        if known and not any(re.search(rf"\b{p}\b", latex.fold(name)) for p in known):
            message += f" The entry names {known[0].upper() if len(known[0]) <= 5 else known[0].title()}."
    elif owner.ok:
        message += f" No publisher at Crossref has the prefix {prefix}."
    return finding("doi_not_found", ERROR, message, field="doi", value=entry.get("doi"), source="doi.org")


def _crossref_extras(entry: Entry, doi: str, src: Sources, out: Lookup) -> None:
    work = src.crossref_work(doi)
    record = work.record
    if not work.ok or record is None:
        return
    if out.record is not None and not out.record.get("abstract") and record.get("abstract"):
        out.record = {**out.record, "abstract": record["abstract"]}
    retracted = False
    for update in record.get("updates") or []:
        kind = str(update.get("type") or "").lower().replace("-", "_")
        when = f" in {update['year']}" if update.get("year") else ""
        url = f"https://doi.org/{update['doi']}" if update.get("doi") else None
        if kind in RETRACTIONS and not retracted:
            retracted = True
            what = "withdrawn" if kind == "withdrawal" else "retracted"
            out.findings.append(finding("retracted", ERROR, f"The work was {what}{when}.", source="crossref", url=url))
        elif kind in CORRECTIONS:
            label = "An expression of concern" if kind == "expression_of_concern" else f"A {kind.replace('_', ' ')}"
            out.findings.append(finding("corrected", INFO, f"{label} was published{when}.", source="crossref", url=url))
    for published in record.get("preprint_of") or []:
        doi_published = normalize_doi(published)
        if doi_published and is_preprint(entry):
            out.findings.append(finding("published", INFO, f"The preprint has been published, as {doi_published}.",
                                        field="doi", record_value=doi_published, source="crossref",
                                        url=f"https://doi.org/{doi_published}"))
            break


def _published_version(entry: Entry, record: dict, src: Sources, out: Lookup) -> None:
    arxiv = record.get("arxiv") or ""
    if not arxiv:
        return
    paper = src.s2_paper(f"ARXIV:{arxiv}")
    if paper.ok and paper.record and not out.record.get("abstract") and paper.record.get("abstract"):
        out.record = {**out.record, "abstract": paper.record["abstract"]}
    if not is_preprint(entry) or not paper.record:
        return
    published = normalize_doi(paper.record.get("doi") or "")
    if published and not published.startswith("10.48550/"):
        venue = paper.record.get("venue") or "a venue"
        out.findings.append(finding("published", INFO, f"The preprint has been published in {venue}, as {published}.",
                                    field="doi", record_value=published, source="semantic scholar",
                                    url=f"https://doi.org/{published}"))


def _by_isbn(entry: Entry, ids: Ids, src: Sources, out: Lookup) -> None:
    answer = src.openlibrary_isbn(ids.isbn or "")
    if answer.ok and answer.record:
        record = answer.record
        bt, rt = entry.get("title") or "", record.get("title") or ""
        if bt and rt and latex.similarity(bt, rt) < 0.6 and not latex.is_prefix_title(bt, rt):
            out.findings.append(finding("isbn", WARNING, f"The ISBN belongs to another book, {_describe(record)}.",
                                        field="isbn", value=entry.get("isbn"), source="openlibrary", url=record.get("url")))
            _search(entry, src, out, quiet=True)
            return
        findings, _ = compare(entry, record, from_doi=False)
        # Editions differ in year and Open Library lists editors as authors,
        # so only the title and authors that do not match at all count.
        out.findings += [f for f in findings if f["code"] == "title" or (f["code"] == "authors" and f["level"] == ERROR)]
        out.record = record
        return
    _search(entry, src, out)


def _hit(entry: Entry, record: dict) -> bool:
    bt, rt = entry.get("title") or "", clean_title(record.get("title") or "")
    if not bt or not rt:
        return False
    if latex.similarity(bt, rt) < 0.9 and not latex.is_prefix_title(bt, rt):
        return False
    mine, _ = families(entry)
    theirs = record_families(record)
    return not mine or not theirs or bool(set(mine) & set(theirs))


def _search(entry: Entry, src: Sources, out: Lookup, *, quiet: bool = False, suggest_reason: str = "") -> None:
    """Look for the title in every index. ``quiet`` adds no finding when
    nothing is found, for a search that only looks for the right DOI."""
    title = latex.to_text(entry.get("title") or "")
    if not title:
        return
    names, _ = latex.split_names(entry.get("author") or entry.get("editor") or "")
    first = latex.to_text(latex.parse_name(names[0]).last) if names else ""
    year = entry_year(entry)
    asks = []
    if entry.type in BOOK_TYPES:
        asks.append(("openlibrary", lambda: src.openlibrary_search(title)))
    asks += [
        ("crossref", lambda: src.crossref_search(title, first, int(year) if year.isdigit() else None)),
        ("semantic scholar", lambda: src.s2_match(title)),
        ("dblp", lambda: src.dblp_search(title)),
        ("arxiv", lambda: src.arxiv_search(title)),
    ]
    answered: list[str] = []
    closest: tuple[float, dict] | None = None
    # The indexes hold copies of famous papers under other DOIs and years,
    # so a hit of the entry's year wins over one of another year.
    fallback: dict | None = None
    best: dict | None = None
    for name, ask in asks:
        answer: Answer = ask()
        if not answer.ok:
            continue
        answered.append(name)
        for record in answer.records:
            if _hit(entry, record):
                if _same_year(year, record):
                    best = record
                    break
                fallback = fallback or record
                continue
            sim = latex.similarity(title, clean_title(record.get("title") or ""))
            if sim >= 0.75 and (closest is None or sim > closest[0]):
                closest = (sim, record)
        if best is not None:
            break
    record = best or fallback
    if record is not None:
        if not quiet:
            findings, _ = compare(entry, record, from_doi=False)
            out.findings += findings
            out.record = record
        if _suggestable(entry, record, year) and (suggest_reason or not entry.get("doi")):
            text = (suggest_reason or "The work has the DOI {doi}.").format(doi=record["doi"])
            out.findings.append(finding("suggest_doi", INFO, text, field="doi", record_value=record["doi"],
                                        source=record["source"], url=f"https://doi.org/{record['doi']}"))
        return
    if quiet:
        return
    asked = _list([SOURCE_NAMES[n] for n in answered])
    if len(answered) < 2:
        out.findings.append(finding("unavailable", INFO,
                                    f"Too few sources answered to tell whether the work exists (only {asked or 'none'}). "
                                    "Check again later.", source="search"))
        return
    level = ERROR if entry.type in INDEXED_TYPES else WARNING
    message = f"No source has a work with this title. {asked} were asked."
    if closest is not None:
        message += f" The closest is {_describe(closest[1])}."
    message += _author_probe(names[0] if names else "", title, src)
    out.findings.append(finding("not_found", level, message, field="title", value=entry.get("title"), source="search"))


def _same_year(year: str, record: dict) -> bool:
    years = [y for y in record.get("years") or [] if isinstance(y, int)]
    return not year.isdigit() or not years or int(year) in years


def _suggestable(entry: Entry, record: dict, year: str) -> bool:
    """Whether a DOI of a hit may be offered: the hit is of the entry's year,
    and an arXiv DOI only for an entry that cites the preprint."""
    doi = record.get("doi") or ""
    if not doi or not _same_year(year, record):
        return False
    return not doi.startswith("10.48550/") or is_preprint(entry)


def _author_probe(name: str, title: str, src: Sources) -> str:
    """What Semantic Scholar knows of the first author, for entries no index
    has. Real names with a made-up title are a common invention."""
    if not name:
        return ""
    person = latex.parse_name(name)
    full = latex.to_text(" ".join(p for p in (person.first, person.von, person.last) if p))
    if len(full.split()) < 2:
        return ""
    answer = src.s2_author_titles(full)
    if not answer.ok:
        return ""
    if not answer.info:
        return f" Semantic Scholar knows no author called {full}."
    titles = answer.info.get("titles") or []
    best = max(titles, key=lambda t: latex.similarity(title, str(t[0] or "")), default=None)
    if best and latex.similarity(title, str(best[0] or "")) >= 0.8:
        when = f" ({best[1]})" if best[1] else ""
        return f" Semantic Scholar has “{best[0]}”{when} by {answer.info.get('name') or full}, which is close."
    count = answer.info.get("count") or len(titles)
    return f" {answer.info.get('name') or full} has {count} papers on Semantic Scholar, and none has a similar title."


def _links(entry: Entry, ids: Ids, src: Sources, out: Lookup) -> None:
    urldate = re.sub(r"\D", "", (entry.get("urldate") or "")[:10])
    for url in ids.urls[:2]:
        answer = src.page(url)
        info = answer.info
        status, error = info.get("status"), info.get("error") or ""
        host = (urlsplit(url).hostname or "").lower()
        if not answer.ok:
            out.findings.append(finding("url_blocked", INFO, f"meertex does not fetch this link. {info.get('detail') or ''}".strip(),
                                        field="url", value=url, source="web"))
            continue
        dead = error in ("dns", "refused", "tls") or status in (404, 410)
        moved = False
        if not dead and status is not None and 200 <= status < 300:
            heading = f"{info.get('title') or ''} {info.get('h1') or ''}"
            path = urlsplit(url).path
            final = urlsplit(info.get("final_url") or url)
            if NOT_FOUND_PAGE.search(heading):
                moved = True
                why = f"The page answers, but says “{(info.get('title') or info.get('h1') or '').strip()[:100]}”."
            elif path not in ("", "/") and final.path in ("", "/", "/index.html", "/index.php"):
                moved = True
                why = f"The link ends on the site's home page, {info.get('final_url')}."
        if dead or moved:
            if dead and host in ("github.com", "www.github.com") and status == 404:
                message = "GitHub has no public repository or page at this link. It does not exist, or it is private."
            elif dead and status:
                message = f"The link is dead ({status})."
            elif dead:
                message = f"The link is dead. {info.get('detail') or ''}".strip()
            else:
                message = why
            archived = src.wayback(url, urldate)
            snapshot = archived.info.get("url") if archived.ok else None
            if snapshot:
                stamp = str(archived.info.get("timestamp") or "")
                when = f" from {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else ""
                message += f" The Wayback Machine has a copy{when}."
            out.findings.append(finding("url_dead" if dead else "url_moved", ERROR if dead else WARNING, message,
                                        field="url", value=url, source="wayback" if snapshot else "web",
                                        url=snapshot or None))
            continue
        if status is None or not 200 <= status < 300:
            what = f"answered {status}" if status else ("took too long" if error == "timeout" else "could not be read")
            out.findings.append(finding("url_blocked", INFO, f"The site {what}, so meertex cannot tell whether the page "
                                        "is there. Sites with bot protection answer like that.", field="url", value=url,
                                        source="web"))
            continue
        page = info.get("record") or {}
        title = latex.to_text(entry.get("title") or "")
        page_title = page.get("title") or info.get("title") or ""
        if title and page_title:
            a = {w for w in latex.words(title) if len(w) >= 3 and w not in build.STOP_WORDS}
            b = {w for w in latex.words(page_title) if len(w) >= 3 and w not in build.STOP_WORDS}
            if a and b and not (a & b):
                out.findings.append(finding("url_title", INFO, f"The page's title is “{page_title[:150]}”.",
                                            field="url", value=url, source="web"))
        if out.record is None:
            out.record = {**page, "url": info.get("final_url") or url} if page else {
                "source": "web", "url": info.get("final_url") or url, "title": page_title, "authors": [], "people": [],
                "year": None, "years": [], "venue": "", "type": "webpage", "doi": "", "volume": "", "number": "",
                "pages": "", "publisher": "", "abstract": "", "arxiv": "", "isbn": ""}
