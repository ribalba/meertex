"""Lookups for the reference check (docs/DESIGN.md §16): doi.org, Crossref,
Semantic Scholar, DBLP, arXiv, Open Library, the Wayback Machine and the links
a .bib names.

Every request goes through ``Http``:

* Only http and https, and only to public addresses. The host is resolved
  before each request, every redirect is followed by hand, and the address
  the connection really went to is checked again before the body is read.
  Anyone who can edit a .bib chooses the links, and the server sits next to
  its database and the compile service.
* Each host gets its own pace, and 429s and server errors of the services
  are tried again after a pause.
* At most ``MAX_BYTES`` of a body are read.

``Sources`` asks the services and keeps their answers in ``reference_cache``,
shared by all projects. A lookup returns an ``Answer``: whether the service
answered at all, and the records it gave in one shape (see ``_record``).
"""

import ipaddress
import json
import logging
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..config import get_settings

logger = logging.getLogger("uvicorn.error")

MAX_BYTES = 1024 * 1024
# A service's record of a paper with thousands of authors is larger.
MAX_API_BYTES = 8 * 1024 * 1024
# arXiv's HTML of a long paper is a few MB, the claim check keeps 300,000 characters.
MAX_FULLTEXT_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRY_STATUS = {429, 500, 502, 503, 504}
BREAK_AFTER = 3

# Seconds between two requests to one host. arXiv asks for three.
PACE = {
    "api.semanticscholar.org": 1.1,
    "dblp.org": 1.0,
    "export.arxiv.org": 3.0,
    "arxiv.org": 1.0,
    "openlibrary.org": 0.5,
    "archive.org": 0.5,
    "api.crossref.org": 0.05,
    "doi.org": 0.05,
}
DEFAULT_PACE = 0.5

# How long an answer stays in the cache. Failures are asked again soon.
TTL_RECORD = timedelta(days=7)
TTL_PAGE = timedelta(days=1)
TTL_CLAIM = timedelta(days=30)
TTL_FAILURE = timedelta(hours=1)
CACHE_VERSION = "1"

_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def _version() -> str:
    try:
        return _VERSION_FILE.read_text().strip() or "dev"
    except OSError:
        return "dev"


def user_agents() -> tuple[str, str]:
    """The User-Agent for the services (with a mailto, for Crossref's polite
    pool) and the one for web pages (a crawler's usual form)."""
    settings = get_settings()
    mailto = settings.references_mailto.strip() or settings.email_from
    base = settings.base_url.rstrip("/")
    version = _version()
    return (
        f"meertex/{version} (+{base}; mailto:{mailto})",
        f"Mozilla/5.0 (compatible; meertex/{version}; +{base})",
    )


# --- Fetching ---------------------------------------------------------------------


class Refused(Exception):
    """A link meertex does not fetch: a scheme other than http or https, or
    a host with an address that is not public."""


@dataclass
class Fetched:
    """What one GET gave. ``error`` is empty when there was an answer, else
    one of ``scheme``, ``private``, ``dns``, ``refused``, ``tls``,
    ``timeout``, ``redirects`` or ``error``."""

    url: str
    final_url: str = ""
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str = ""
    detail: str = ""

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8", errors="replace"))

    def text(self) -> str:
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.IGNORECASE)
        if m:
            charset = m.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def is_public(address: str) -> bool:
    """Whether an IP address is on the public internet: not loopback,
    private, link-local, shared (100.64/10), multicast or reserved."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _resolve(host: str, port: int) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


_pace_guard = threading.Lock()
_next_at: dict[str, float] = {}


class Http:
    """GET requests with the rules of this module. ``transport`` and
    ``resolve`` are for tests. With a test transport the connected address
    cannot be read, so only the resolved one is checked."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        resolve: Callable[[str, int], list[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._strict = transport is None
        self._resolve = resolve or _resolve
        self._sleep = sleep
        # No proxy from the environment, so the address checks mean what they say.
        self._client = httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def _check_host(self, host: str, port: int) -> None:
        try:
            addresses = [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            addresses = self._resolve(host, port)
        if not addresses or not all(is_public(a) for a in addresses):
            raise Refused(f"{host} is not on the public internet")

    def _check_peer(self, resp: httpx.Response) -> None:
        stream = resp.extensions.get("network_stream")
        if stream is None:
            if self._strict:
                raise Refused("The address of the connection is unknown")
            return
        peer = stream.get_extra_info("server_addr")
        if not peer or not is_public(str(peer[0])):
            raise Refused("The connection went to an address that is not public")

    def _pace(self, host: str) -> None:
        interval = PACE.get(host, DEFAULT_PACE)
        with _pace_guard:
            now = time.monotonic()
            at = max(now, _next_at.get(host, 0.0))
            _next_at[host] = at + interval
        if at > now:
            self._sleep(at - now)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        retries: int = 2,
        max_bytes: int = MAX_BYTES,
        retry_status: set[int] = RETRY_STATUS,
    ) -> Fetched:
        """GET ``url``, following up to MAX_REDIRECTS redirects. Never raises
        for network trouble, the result's ``error`` says what went wrong."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return Fetched(url, current, error="scheme", detail="Only http and https links are fetched.")
            try:
                port = parts.port or (443 if parts.scheme == "https" else 80)
            except ValueError:
                return Fetched(url, current, error="scheme", detail="The link has a port that is not a number.")
            try:
                self._check_host(parts.hostname, port)
            except Refused as e:
                return Fetched(url, current, error="private", detail=str(e))
            except (socket.gaierror, UnicodeError) as e:
                return Fetched(url, current, error="dns", detail=f"{parts.hostname} does not exist ({e}).")
            result = self._one(url, current, parts.hostname.lower(), headers, retries, max_bytes, retry_status)
            if isinstance(result, Fetched):
                return result
            current = result
        return Fetched(url, current, error="redirects", detail=f"More than {MAX_REDIRECTS} redirects.")

    def _one(
        self,
        url: str,
        current: str,
        host: str,
        headers: dict[str, str] | None,
        retries: int,
        max_bytes: int,
        retry_status: set[int],
    ) -> Fetched | str:
        """One request, tried again on a retryable status. Returns the
        result, or the next URL of a redirect."""
        attempt = 0
        while True:
            self._pace(host)
            try:
                with self._client.stream("GET", current, headers=headers) as resp:
                    self._check_peer(resp)
                    location = resp.headers.get("location")
                    if resp.is_redirect and location:
                        return urljoin(current, location)
                    if resp.status_code in retry_status and attempt < retries:
                        wait = _retry_after(resp.headers.get("retry-after"), attempt)
                    else:
                        body = bytearray()
                        for chunk in resp.iter_bytes():
                            body.extend(chunk)
                            if len(body) >= max_bytes:
                                del body[max_bytes:]
                                break
                        return Fetched(url, current, resp.status_code, dict(resp.headers), bytes(body))
            except Refused as e:
                return Fetched(url, current, error="private", detail=str(e))
            except httpx.TimeoutException as e:
                if attempt < min(retries, 1):
                    attempt += 1
                    continue
                return Fetched(url, current, error="timeout", detail=f"No answer in time ({e.__class__.__name__}).")
            except httpx.ConnectError as e:
                return Fetched(url, current, error=_connect_error(e), detail=str(e) or e.__class__.__name__)
            except httpx.HTTPError as e:
                return Fetched(url, current, error="error", detail=f"{e.__class__.__name__}: {e}")
            attempt += 1
            self._sleep(wait)


def _retry_after(value: str | None, attempt: int) -> float:
    if value and value.strip().isdigit():
        return min(float(value.strip()), 10.0)
    return 2.0 * (attempt + 1)


def _connect_error(e: httpx.ConnectError) -> str:
    text = str(e).lower()
    if "ssl" in text or "certificate" in text or "tls" in text:
        return "tls"
    if "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
        return "dns"
    return "refused"


# --- The cache --------------------------------------------------------------------


class Cache:
    """Answers by key. The database one is shared by all projects and
    threads, the memory one is for tests."""

    def get(self, key: str) -> tuple[bool, object] | None:
        raise NotImplementedError

    def put(self, key: str, ok: bool, value: object) -> None:
        raise NotImplementedError


def _ttl(key: str, ok: bool) -> timedelta:
    if not ok:
        return TTL_FAILURE
    if key.startswith(("page:", "wayback:")):
        return TTL_PAGE
    if key.startswith("claim:"):
        return TTL_CLAIM
    return TTL_RECORD


class MemoryCache(Cache):
    def __init__(self) -> None:
        self.data: dict[str, tuple[bool, object]] = {}

    def get(self, key: str) -> tuple[bool, object] | None:
        return self.data.get(key)

    def put(self, key: str, ok: bool, value: object) -> None:
        self.data[key] = (ok, value)


class DbCache(Cache):
    """``reference_cache``. ``refresh`` ignores what is there, for a run that
    asks again."""

    def __init__(self, refresh: bool = False) -> None:
        self.refresh = refresh

    def get(self, key: str) -> tuple[bool, object] | None:
        if self.refresh:
            return None
        from ..database import SessionLocal
        from ..models import ReferenceCache, utcnow

        with SessionLocal() as db:
            row = db.get(ReferenceCache, key)
            if row is None or row.fetched_at < utcnow() - _ttl(key, row.ok):
                return None
            return row.ok, row.value

    def put(self, key: str, ok: bool, value: object) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from ..database import SessionLocal
        from ..models import ReferenceCache, utcnow

        now = utcnow()
        stmt = insert(ReferenceCache).values(key=key, ok=ok, value=value, fetched_at=now)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"ok": ok, "value": value, "fetched_at": now})
        with SessionLocal() as db:
            db.execute(stmt)
            db.commit()


def prune_cache() -> None:
    """Remove cache rows older than 30 days."""
    from ..database import SessionLocal
    from ..models import ReferenceCache, utcnow

    with SessionLocal() as db:
        db.query(ReferenceCache).filter(ReferenceCache.fetched_at < utcnow() - timedelta(days=30)).delete()
        db.commit()


# --- Records ----------------------------------------------------------------------


@dataclass
class Answer:
    """What one service said. ``ok`` is false when it did not answer (down,
    rate limited, a broken reply), which tells "unknown" from "not there".
    ``info`` carries what is not a record (a page, a prefix's owner)."""

    ok: bool
    records: list[dict] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> dict:
        return {"ok": self.ok, "records": self.records, "info": self.info, "error": self.error}

    @classmethod
    def from_json(cls, data: dict) -> "Answer":
        return cls(bool(data.get("ok")), list(data.get("records") or []), dict(data.get("info") or {}), str(data.get("error") or ""))

    @property
    def record(self) -> dict | None:
        return self.records[0] if self.records else None


def _record(source: str, **values: object) -> dict:
    """A record in the one shape every source gives. ``people`` are
    ``{family, given}``, ``authors`` the same names as text, ``years`` every
    year the work carries (issued, in print, online), ``year`` the first."""
    record = {
        "source": source,
        "doi": "",
        "url": "",
        "type": "",
        "title": "",
        "authors": [],
        "people": [],
        "year": None,
        "years": [],
        "venue": "",
        "volume": "",
        "number": "",
        "pages": "",
        "publisher": "",
        "abstract": "",
        "arxiv": "",
        "isbn": "",
    }
    record.update(values)
    record["authors"] = [_display(p) for p in record["people"]]
    if record["years"] and record["year"] is None:
        record["year"] = record["years"][0]
    # Records keep the publisher's spelling, which a new entry writes.
    record["doi"] = normalize_doi(str(record["doi"] or ""), lower=False) or ""
    return record


def _display(person: dict) -> str:
    return " ".join(p for p in (person.get("given", ""), person.get("family", "")) if p).strip()


def _first(value: object) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _people_csl(authors: object) -> list[dict]:
    people = []
    for a in authors if isinstance(authors, list) else []:
        if not isinstance(a, dict):
            continue
        family = " ".join(p for p in (a.get("non-dropping-particle", ""), a.get("family", "")) if p).strip()
        if not family:
            family = str(a.get("literal") or a.get("name") or "").strip()
        if family:
            people.append({"family": family, "given": str(a.get("given") or "").strip()})
    return people


def _people_names(names: list[str]) -> list[dict]:
    """Full names ("Ashish Vaswani") as family and given names."""
    people = []
    for name in names:
        name = re.sub(r"\s+\d{4}$", "", name.strip())  # DBLP's homonym numbers
        if not name:
            continue
        if "," in name:
            family, _, given = name.partition(",")
        else:
            given, _, family = name.rpartition(" ")
        people.append({"family": family.strip(), "given": given.strip()})
    return people


def _years_csl(data: dict) -> list[int]:
    years: list[int] = []
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (data.get(key) or {}).get("date-parts") if isinstance(data.get(key), dict) else None
        if parts and parts[0] and parts[0][0]:
            try:
                year = int(parts[0][0])
            except (TypeError, ValueError):
                continue
            if year not in years:
                years.append(year)
    return years


def strip_markup(text: str) -> str:
    """Text without HTML or JATS tags and without runs of white space."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()
    return re.sub(r"\s+([.,;:!?)])", r"\1", text)


def _from_csl(data: dict, source: str) -> dict:
    """A record from CSL JSON (doi.org) or Crossref's works JSON."""
    venue = _first(data.get("container-title"))
    event = data.get("event")
    if not venue:
        venue = _first(event.get("name") if isinstance(event, dict) else event) or _first(data.get("event-title"))
    pages = _first(data.get("page"))
    if not pages and data.get("article-number"):
        pages = _first(data.get("article-number"))
    doi = _first(data.get("DOI"))
    arxiv = ""
    m = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.IGNORECASE)
    if m:
        arxiv = m.group(1)
    return _record(
        source,
        doi=doi,
        url=_first(data.get("URL")),
        type=_first(data.get("type")),
        title=strip_markup(_first(data.get("title"))),
        people=_people_csl(data.get("author")),
        years=_years_csl(data),
        venue=strip_markup(venue),
        volume=_first(data.get("volume")),
        number=_first(data.get("issue")),
        pages=pages,
        publisher=_first(data.get("publisher")),
        abstract=strip_markup(_first(data.get("abstract"))),
        arxiv=arxiv,
    )


def normalize_doi(value: str, lower: bool = True) -> str | None:
    """The DOI in a value (``10.1145/x``, ``doi:10.1145/x``, a doi.org link),
    or None. Lowercased unless ``lower`` is false, because DOIs ignore case."""
    value = (value or "").strip()
    value = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    value = value.strip().rstrip(".")
    if not re.match(r"^10\.\d{4,9}/\S+$", value):
        return None
    return value.lower() if lower else value


class _Html(HTMLParser):
    """The title, the meta tags, the first heading, JSON-LD and, when asked,
    the text of a page."""

    SKIP = frozenset({"script", "style", "noscript", "template", "svg", "nav", "header", "footer"})

    def __init__(self, want_text: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.want_text = want_text
        self.title = ""
        self.h1 = ""
        self.meta: dict[str, list[str]] = {}
        self.jsonld: list[str] = []
        self.parts: list[str] = []
        self._in: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            if name and "content" in a:
                self.meta.setdefault(name, []).append(a["content"].strip())
            return
        if tag in ("title", "h1") or (tag == "script" and a.get("type", "").lower() == "application/ld+json"):
            self._in.append(tag if tag != "script" else "ld")
            self._buf = []
        elif tag in self.SKIP:
            self._in.append("skip")
        if self.want_text and tag in ("p", "div", "li", "h2", "h3", "h4", "section", "br", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._in:
            return
        top = self._in[-1]
        if (top == "ld" and tag == "script") or top == tag or (top == "skip" and tag in self.SKIP):
            self._in.pop()
            text = "".join(self._buf).strip()
            if top == "title" and not self.title:
                self.title = re.sub(r"\s+", " ", text)
            elif top == "h1" and not self.h1:
                self.h1 = re.sub(r"\s+", " ", text)
            elif top == "ld":
                self.jsonld.append(text)
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in and self._in[-1] in ("title", "h1", "ld"):
            self._buf.append(data)
            if self._in[-1] != "h1":
                return
        if self.want_text and not any(t in ("skip", "ld", "title") for t in self._in):
            self.parts.append(data)

    def text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def scan_html(html: str, want_text: bool = False) -> _Html:
    page = _Html(want_text)
    try:
        page.feed(html)
        page.close()
    except Exception as e:  # noqa: BLE001  (a broken page still gives what was read)
        logger.debug("A page did not parse to its end: %s", e)
    return page


def _jsonld_objects(blocks: list[str]) -> list[dict]:
    out: list[dict] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            out.append(value)
            for key in ("@graph", "mainEntity"):
                walk(value.get(key))
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return out


def page_record(url: str, page: _Html) -> dict:
    """What a web page says about itself: Highwire's ``citation_*`` meta tags
    (publishers, arXiv, Google Scholar), JSON-LD, Open Graph and the title."""
    meta = page.meta
    one = lambda *names: next((meta[n][0] for n in names if meta.get(n) and meta[n][0]), "")
    title = one("citation_title", "dc.title", "og:title", "twitter:title")
    people = _people_names(meta.get("citation_author", []) or meta.get("dc.creator", []))
    date = one("citation_publication_date", "citation_date", "citation_online_date", "dc.date",
               "article:published_time", "date")
    venue = one("citation_journal_title", "citation_conference_title", "citation_inbook_title", "og:site_name")
    publisher = one("citation_publisher", "dc.publisher")
    doi = one("citation_doi", "dc.identifier", "prism.doi")
    for obj in _jsonld_objects(page.jsonld):
        kind = obj.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(k in ("Article", "ScholarlyArticle", "BlogPosting", "NewsArticle", "Report", "WebPage",
                         "TechArticle", "SoftwareSourceCode", "Dataset", "Book") for k in kinds):
            continue
        title = title or str(obj.get("headline") or obj.get("name") or "").strip()
        date = date or str(obj.get("datePublished") or obj.get("dateCreated") or "").strip()
        if not people:
            authors = obj.get("author")
            authors = authors if isinstance(authors, list) else [authors] if authors else []
            names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in authors]
            people = _people_names([n for n in names if n])
        if not publisher and isinstance(obj.get("publisher"), dict):
            publisher = str(obj["publisher"].get("name") or "")
    year = None
    m = re.search(r"(1[5-9]\d\d|20\d\d)", date or "")
    if m:
        year = int(m.group(1))
    return _record(
        "web",
        doi=doi,
        url=url,
        type="webpage",
        title=strip_markup(title or page.title),
        people=people,
        years=[year] if year else [],
        venue=venue,
        publisher=publisher,
        abstract=strip_markup(one("citation_abstract", "dc.description", "description", "og:description")),
    )


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def _arxiv_records(xml: str) -> list[dict]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ValueError("arXiv sent XML that does not parse") from None
    records = []
    for entry in root.findall(f"{ATOM}entry"):
        ident = (entry.findtext(f"{ATOM}id") or "").strip()
        m = re.search(r"arxiv\.org/abs/(.+?)(v\d+)?$", ident)
        if not m:
            continue  # arXiv's error entries
        arxiv_id = m.group(1)
        published = entry.findtext(f"{ATOM}published") or ""
        years = [int(published[:4])] if published[:4].isdigit() else []
        names = [(a.findtext(f"{ATOM}name") or "").strip() for a in entry.findall(f"{ATOM}author")]
        records.append(_record(
            "arxiv",
            doi=f"10.48550/arXiv.{arxiv_id}",
            url=f"https://arxiv.org/abs/{arxiv_id}",
            type="preprint",
            title=re.sub(r"\s+", " ", entry.findtext(f"{ATOM}title") or "").strip(),
            people=_people_names(names),
            years=years,
            venue="arXiv",
            publisher="arXiv",
            abstract=re.sub(r"\s+", " ", entry.findtext(f"{ATOM}summary") or "").strip(),
            arxiv=arxiv_id,
            published_doi=(entry.findtext(f"{ARXIV_NS}doi") or "").strip(),
        ))
    return records


# --- The services -----------------------------------------------------------------


class Sources:
    """Every lookup the check makes, cached. Each method returns an Answer."""

    def __init__(self, http: Http | None = None, cache: Cache | None = None) -> None:
        self.http = http or Http()
        self.cache = cache or MemoryCache()
        self.api_agent, self.page_agent = user_agents()
        self.s2_key = get_settings().semantic_scholar_api_key.strip()
        # Failures in a row per service. A service that failed BREAK_AFTER
        # times is not asked again by this Sources, so a bot wall or an
        # outage does not slow a whole run down.
        self._failures: dict[str, int] = {}
        self._guard = threading.Lock()

    def close(self) -> None:
        self.http.close()

    # -- plumbing

    def _cached(self, key: str, ask: Callable[[], Answer]) -> Answer:
        service = key.split(":", 1)[0]
        key = f"v{CACHE_VERSION}:{key}"
        hit = self.cache.get(key)
        if hit is not None and isinstance(hit[1], dict):
            return Answer.from_json(hit[1])
        breaks = service not in ("page", "wayback")  # links go to many different hosts
        with self._guard:
            if breaks and self._failures.get(service, 0) >= BREAK_AFTER:
                return Answer(False, error=f"The service failed {BREAK_AFTER} times in a row and was not asked again.")
        try:
            answer = ask()
        except (ValueError, KeyError, TypeError) as e:
            answer = Answer(False, error=f"The answer could not be read ({e.__class__.__name__}: {e}).")
        if breaks:
            with self._guard:
                self._failures[service] = 0 if answer.ok else self._failures.get(service, 0) + 1
        try:
            self.cache.put(key, answer.ok, answer.to_json())
        except Exception:
            logger.exception("Could not cache %s", key)
        return answer

    def _get(self, url: str, accept: str = "application/json", **kwargs: object) -> Fetched:
        headers = {"User-Agent": self.api_agent, "Accept": accept}
        if "api.semanticscholar.org" in url and self.s2_key:
            headers["x-api-key"] = self.s2_key
        kwargs.setdefault("max_bytes", MAX_API_BYTES)
        return self.http.get(url, headers, **kwargs)

    @staticmethod
    def _failed(f: Fetched, what: str) -> Answer:
        if f.error:
            return Answer(False, error=f"{what} could not be reached ({f.detail or f.error}).")
        return Answer(False, error=f"{what} answered {f.status}.")

    # -- doi.org and Crossref

    def doi(self, doi: str) -> Answer:
        """The DOI's record as CSL JSON. ``ok`` with no record means doi.org
        does not know the DOI."""
        doi = normalize_doi(doi) or doi

        def ask() -> Answer:
            f = self._get(f"https://doi.org/{quote(doi, safe='/')}", "application/vnd.citationstyles.csl+json")
            if f.status == 404:
                return Answer(True)
            if f.status != 200:
                return self._failed(f, "doi.org")
            data = f.json()
            if not isinstance(data, dict):
                # A registration agency without CSL JSON sends its landing page.
                return Answer(True, info={"no_csl": True})
            return Answer(True, [_from_csl(data, "doi.org")])

        return self._cached(f"doi:{doi}", ask)

    def crossref_work(self, doi: str) -> Answer:
        """Crossref's own record, for retractions, corrections, relations and
        the abstract. ``ok`` with no record means it is no Crossref DOI."""
        doi = normalize_doi(doi) or doi

        def ask() -> Answer:
            f = self._get(f"https://api.crossref.org/works/{quote(doi, safe='/')}")
            if f.status == 404:
                return Answer(True)
            if f.status != 200:
                return self._failed(f, "Crossref")
            msg = f.json()["message"]
            record = _from_csl(msg, "crossref")
            record["updates"] = [
                {"type": str(u.get("type") or ""), "doi": str(u.get("DOI") or ""), "label": str(u.get("label") or ""),
                 "year": ((u.get("updated") or {}).get("date-parts") or [[None]])[0][0]}
                for u in msg.get("updated-by") or [] if isinstance(u, dict)
            ]
            relation = msg.get("relation") or {}
            record["preprint_of"] = [
                str(r.get("id") or "") for r in relation.get("is-preprint-of") or []
                if isinstance(r, dict) and r.get("id-type") == "doi"
            ]
            return Answer(True, [record])

        return self._cached(f"crossref:{doi}", ask)

    def crossref_prefix(self, prefix: str) -> Answer:
        """Who owns a DOI prefix, as ``info["name"]``."""

        def ask() -> Answer:
            f = self._get(f"https://api.crossref.org/prefixes/{quote(prefix, safe='')}")
            if f.status == 404:
                return Answer(True)
            if f.status != 200:
                return self._failed(f, "Crossref")
            return Answer(True, info={"name": str(f.json()["message"].get("name") or "")})

        return self._cached(f"prefix:{prefix}", ask)

    def crossref_search(self, title: str, author: str = "", year: int | None = None) -> Answer:
        query = " ".join(p for p in (title, author, str(year or "")) if p)

        def ask() -> Answer:
            url = (
                "https://api.crossref.org/works?rows=5&query.bibliographic=" + quote(query)
                + "&select=DOI,title,author,issued,published-print,published-online,container-title,"
                "volume,issue,page,publisher,type,URL"
            )
            f = self._get(url)
            if f.status != 200:
                return self._failed(f, "Crossref")
            items = f.json()["message"].get("items") or []
            return Answer(True, [_from_csl(i, "crossref") for i in items if isinstance(i, dict)])

        return self._cached(f"crossref-search:{query.lower()}", ask)

    # -- Semantic Scholar

    S2_FIELDS = "title,year,authors,venue,journal,externalIds,abstract,publicationVenue,url"

    @staticmethod
    def _s2_record(p: dict) -> dict:
        ids = p.get("externalIds") or {}
        journal = p.get("journal") or {}
        venue = str(p.get("venue") or journal.get("name") or (p.get("publicationVenue") or {}).get("name") or "")
        year = p.get("year")
        return _record(
            "semantic scholar",
            doi=str(ids.get("DOI") or ""),
            url=str(p.get("url") or ""),
            type="",
            title=str(p.get("title") or ""),
            people=_people_names([str(a.get("name") or "") for a in p.get("authors") or [] if isinstance(a, dict)]),
            years=[int(year)] if isinstance(year, int) else [],
            venue=venue,
            volume=str(journal.get("volume") or "").strip(),
            pages=str(journal.get("pages") or "").strip(),
            abstract=str(p.get("abstract") or ""),
            arxiv=str(ids.get("ArXiv") or ""),
        )

    def s2_match(self, title: str) -> Answer:
        def ask() -> Answer:
            f = self._get(
                "https://api.semanticscholar.org/graph/v1/paper/search/match?query=" + quote(title)
                + "&fields=" + self.S2_FIELDS
            )
            if f.status == 404:
                return Answer(True)  # "Title match not found"
            if f.status != 200:
                return self._failed(f, "Semantic Scholar")
            return Answer(True, [self._s2_record(p) for p in f.json().get("data") or [] if isinstance(p, dict)])

        return self._cached(f"s2-match:{title.lower()}", ask)

    def s2_paper(self, ident: str) -> Answer:
        """A paper by ``DOI:<doi>`` or ``ARXIV:<id>``."""

        def ask() -> Answer:
            f = self._get(f"https://api.semanticscholar.org/graph/v1/paper/{quote(ident, safe=':/')}?fields={self.S2_FIELDS}")
            if f.status == 404:
                return Answer(True)
            if f.status != 200:
                return self._failed(f, "Semantic Scholar")
            return Answer(True, [self._s2_record(f.json())])

        return self._cached(f"s2-paper:{ident.lower()}", ask)

    def s2_author_titles(self, name: str) -> Answer:
        """The papers of the author Semantic Scholar finds for a name, as
        ``info = {name, count, titles: [[title, year]]}``."""

        def ask() -> Answer:
            f = self._get(
                "https://api.semanticscholar.org/graph/v1/author/search?limit=1&fields=name,paperCount&query="
                + quote(name)
            )
            if f.status != 200:
                return self._failed(f, "Semantic Scholar")
            found = f.json().get("data") or []
            if not found:
                return Answer(True)
            author = found[0]
            g = self._get(
                f"https://api.semanticscholar.org/graph/v1/author/{quote(str(author.get('authorId')), safe='')}"
                "/papers?limit=1000&fields=title,year"
            )
            if g.status != 200:
                return self._failed(g, "Semantic Scholar")
            titles = [[str(p.get("title") or ""), p.get("year")] for p in g.json().get("data") or [] if isinstance(p, dict)]
            return Answer(True, info={"name": author.get("name") or name, "count": author.get("paperCount") or len(titles),
                                      "titles": titles})

        return self._cached(f"s2-author:{name.lower()}", ask)

    # -- DBLP

    def dblp_search(self, title: str) -> Answer:
        def ask() -> Answer:
            # DBLP answers 202 with a waiting page when it is busy.
            f = self._get(
                "https://dblp.org/search/publ/api?format=json&h=5&q=" + quote(title),
                retries=1,
                retry_status=RETRY_STATUS | {202},
            )
            if f.status != 200 or "json" not in f.headers.get("content-type", ""):
                return self._failed(f, "DBLP")
            hits = ((f.json().get("result") or {}).get("hits") or {}).get("hit") or []
            records = []
            for hit in hits:
                info = hit.get("info") or {}
                authors = (info.get("authors") or {}).get("author") or []
                authors = authors if isinstance(authors, list) else [authors]
                names = [a.get("text", "") if isinstance(a, dict) else str(a) for a in authors]
                year = str(info.get("year") or "")
                venue = info.get("venue") or ""
                records.append(_record(
                    "dblp",
                    doi=str(info.get("doi") or ""),
                    url=str(info.get("ee") or info.get("url") or ""),
                    type=str(info.get("type") or ""),
                    title=str(info.get("title") or "").rstrip("."),
                    people=_people_names(names),
                    years=[int(year)] if year.isdigit() else [],
                    venue=" ".join(venue) if isinstance(venue, list) else str(venue),
                    volume=str(info.get("volume") or ""),
                    number=str(info.get("number") or ""),
                    pages=str(info.get("pages") or ""),
                ))
            return Answer(True, records)

        return self._cached(f"dblp:{title.lower()}", ask)

    # -- arXiv

    def arxiv_search(self, title: str) -> Answer:
        words = re.findall(r"[^\W_]+", title)[:20]

        def ask() -> Answer:
            # A phrase, because arXiv drops stop words from single terms.
            query = quote(f'ti:"{" ".join(words)}"')
            f = self._get(f"https://export.arxiv.org/api/query?max_results=5&search_query={query}", "application/atom+xml")
            if f.status != 200:
                return self._failed(f, "arXiv")
            return Answer(True, _arxiv_records(f.text()))

        if not words:
            return Answer(True)
        return self._cached(f"arxiv-search:{' '.join(words).lower()}", ask)

    def arxiv_paper(self, arxiv_id: str) -> Answer:
        def ask() -> Answer:
            f = self._get(f"https://export.arxiv.org/api/query?id_list={quote(arxiv_id, safe='/.')}", "application/atom+xml")
            if f.status == 400:
                return Answer(True)  # an id arXiv calls malformed
            if f.status != 200:
                return self._failed(f, "arXiv")
            return Answer(True, _arxiv_records(f.text()))

        return self._cached(f"arxiv:{arxiv_id.lower()}", ask)

    def arxiv_fulltext(self, arxiv_id: str) -> Answer:
        """arXiv's HTML of a paper as text in ``info["text"]``. Only papers
        from late 2023 on have one."""

        def ask() -> Answer:
            f = self._get(f"https://arxiv.org/html/{quote(arxiv_id, safe='/.')}", "text/html", max_bytes=MAX_FULLTEXT_BYTES)
            if f.status == 404:
                return Answer(True)
            if f.status != 200:
                return self._failed(f, "arXiv")
            html = f.text()
            # The paper is the page's <article>, the rest is arXiv's frame.
            m = re.search(r"<article\b.*</article>", html, re.DOTALL | re.IGNORECASE)
            text = scan_html(m.group(0) if m else html, want_text=True).text()
            return Answer(True, info={"text": text[:300_000]})

        return self._cached(f"arxiv-html:{arxiv_id.lower()}", ask)

    # -- Open Library

    def openlibrary_isbn(self, isbn: str) -> Answer:
        def ask() -> Answer:
            f = self._get(f"https://openlibrary.org/isbn/{quote(isbn, safe='')}.json")
            if f.status == 404:
                return Answer(True)
            if f.status != 200:
                return self._failed(f, "Open Library")
            data = f.json()
            keys = [a.get("key") for a in data.get("authors") or [] if isinstance(a, dict)]
            if not keys:
                # Editions often leave the authors to their work.
                work = _first([w.get("key") for w in data.get("works") or [] if isinstance(w, dict)])
                if re.fullmatch(r"/works/OL\w+", work):
                    w = self._get(f"https://openlibrary.org{work}.json")
                    if w.status == 200:
                        keys = [((a.get("author") or {}).get("key")) for a in w.json().get("authors") or []
                                if isinstance(a, dict)]
            names = []
            for key in keys[:6]:
                if not key or not re.fullmatch(r"/authors/OL\w+", str(key)):
                    continue
                g = self._get(f"https://openlibrary.org{key}.json")
                if g.status == 200:
                    names.append(str(g.json().get("name") or ""))
            m = re.search(r"(1[4-9]\d\d|20\d\d)", str(data.get("publish_date") or ""))
            title = str(data.get("title") or "")
            if data.get("subtitle"):
                title = f"{title}: {data['subtitle']}"
            return Answer(True, [_record(
                "openlibrary",
                url=f"https://openlibrary.org/isbn/{isbn}",
                type="book",
                title=title,
                people=_people_names([n for n in names if n]),
                years=[int(m.group(1))] if m else [],
                publisher=_first(data.get("publishers")),
                isbn=isbn,
            )])

        return self._cached(f"isbn:{isbn}", ask)

    def openlibrary_search(self, title: str) -> Answer:
        def ask() -> Answer:
            f = self._get(
                "https://openlibrary.org/search.json?limit=5&fields=title,subtitle,author_name,first_publish_year,"
                "isbn,publisher,key&title=" + quote(title)
            )
            if f.status != 200:
                return self._failed(f, "Open Library")
            records = []
            for d in f.json().get("docs") or []:
                year = d.get("first_publish_year")
                title_text = str(d.get("title") or "")
                if d.get("subtitle"):
                    title_text = f"{title_text}: {d['subtitle']}"
                records.append(_record(
                    "openlibrary",
                    url=f"https://openlibrary.org{d.get('key', '')}",
                    type="book",
                    title=title_text,
                    people=_people_names([str(n) for n in d.get("author_name") or []]),
                    years=[int(year)] if isinstance(year, int) else [],
                    publisher=_first(d.get("publisher")),
                    isbn=_first(d.get("isbn")),
                ))
            return Answer(True, records)

        return self._cached(f"openlibrary:{title.lower()}", ask)

    # -- The web

    def page(self, url: str) -> Answer:
        """A link as ``info = {url, final_url, status, error, detail, title,
        h1, record}``. ``ok`` is false only when meertex did not even try
        (a scheme or address it refuses)."""

        def ask() -> Answer:
            headers = {"User-Agent": self.page_agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
            f = self.http.get(url, headers, retries=0, retry_status=set())
            info = {"url": url, "final_url": f.final_url or url, "status": f.status, "error": f.error,
                    "detail": f.detail, "title": "", "h1": "", "record": None}
            if f.status is not None and "html" in f.headers.get("content-type", "") and f.body:
                page = scan_html(f.text())
                info["title"] = page.title
                info["h1"] = page.h1
                info["record"] = page_record(f.final_url or url, page)
            return Answer(f.error not in ("scheme", "private"), info=info)

        return self._cached(f"page:{url}", ask)

    def wayback(self, url: str, timestamp: str = "") -> Answer:
        """The Wayback Machine's snapshot closest to ``timestamp``
        (YYYYMMDD), as ``info["url"]``."""

        def ask() -> Answer:
            query = "url=" + quote(url, safe="") + (f"&timestamp={timestamp}" if timestamp else "")
            f = self._get(f"https://archive.org/wayback/available?{query}")
            if f.status != 200:
                return self._failed(f, "The Wayback Machine")
            closest = (f.json().get("archived_snapshots") or {}).get("closest") or {}
            if not closest.get("available") or not closest.get("url"):
                return Answer(True)
            return Answer(True, info={"url": str(closest["url"]).replace("http://", "https://", 1),
                                      "timestamp": str(closest.get("timestamp") or "")})

        return self._cached(f"wayback:{url}|{timestamp}", ask)
