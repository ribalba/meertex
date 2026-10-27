"""The reference check's report, its runs and its writes (docs/DESIGN.md §16).

The report is built on every read from the live files. Parsing and the
checks without the network are cheap, and so the report is never stale.
What a run found is kept per fingerprint (``reference_results``) and joined
in. An entry whose text changed since has another fingerprint and shows as
``pending`` until the next run.

A run works in a thread of its own, like a version's compile. One project
runs one check at a time, held in this process, which is enough because the
server is one process (§1). A run that the database still calls running but
this process does not know was cut off by a restart.

Writes (a new entry, a field, the checked mark) are CRDT edits through the
project's room, made under its lock after reading the current text, so they
never write over an edit that came in meanwhile.
"""

import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession

from .. import live, serialize
from ..config import get_settings
from ..database import SessionLocal
from ..live import rooms, ydoc
from ..models import Project, ReferenceResult, ReferenceRun, User, utcnow
from ..when import iso
from . import bibtex, build, checks, claims, latex, sources
from .bibtex import MARK_FIELD, Entry

logger = logging.getLogger("uvicorn.error")

RESULT_MAX_AGE = timedelta(days=1)
LOOKUP_THREADS = 4
CLAIM_THREADS = 4
PROBLEM_STATES = ("error", "changed", "warning")
STATUS_ORDER = ("error", "changed", "warning", "pending", "unverified", "verified", "checked")
_KEY = re.compile(r"^[^\s,{}()\"#%'=~\\]{1,200}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_.:+-]{0,63}$")


class Unavailable(Exception):
    """The network checks or the claims are off, or a service cannot be
    reached. ``args[0]`` says which, for a 503."""


class Ambiguous(live.Refused):
    """Two entries have the key. ``args[0]`` says where, for a 409."""


# --- Reading the project ------------------------------------------------------------


@dataclass
class Parsed:
    files: list[str]
    entries: list[Entry]
    broken: list[bibtex.Problem]
    cites: list[latex.Cite]
    tex: dict[str, str] = field(default_factory=dict)


def _is_bib(path: str) -> bool:
    return path.lower().endswith(".bib")


def _is_tex(path: str) -> bool:
    return path.lower().endswith((".tex", ".ltx"))


def parse_project(project_id: str) -> Parsed:
    with rooms.get(project_id).read() as doc:
        files = ydoc.materialize(doc)
        texts = {p: e.text or "" for p, e in files.items() if e.kind == "text" and (_is_bib(p) or _is_tex(p))}
    return _parse(texts)


def _parse(texts: dict[str, str]) -> Parsed:
    bibs = sorted(p for p in texts if _is_bib(p))
    entries: list[Entry] = []
    broken: list[bibtex.Problem] = []
    for path in bibs:
        parsed = bibtex.parse(texts[path], path)
        entries += parsed.entries
        broken += parsed.problems
    bibtex.resolve_crossrefs(entries)
    tex = {p: t for p, t in texts.items() if _is_tex(p)}
    cites = [c for path in sorted(tex) for c in latex.cites(tex[path], path)]
    return Parsed(bibs, entries, broken, cites, tex)


# --- The report ---------------------------------------------------------------------


def _today() -> date:
    return utcnow().date()


def _title_key(entry: Entry) -> str:
    words = [w for w in latex.words(entry.get("title") or "") if w not in build.STOP_WORDS]
    return " ".join(words) if len(words) >= 3 else ""


def _status(findings: list[dict], mark: dict | None, result: ReferenceResult | None) -> str:
    levels = {f["level"] for f in findings}
    if any(f["code"] == "retracted" for f in findings):
        return "error"
    if mark is not None:
        return "checked" if mark["valid"] else "changed"
    if "error" in levels:
        return "error"
    if "warning" in levels:
        return "warning"
    if result is None:
        return "pending"
    return "verified" if result.record else "unverified"


def _mark(entry: Entry, fingerprint: str) -> dict | None:
    f = entry.field(MARK_FIELD)
    if f is None:
        return None
    mark = bibtex.parse_mark(f.value)
    if mark is None:
        return {"date": "", "by": "", "hash": "", "valid": False}
    return {"date": mark.date, "by": mark.by, "hash": mark.hash, "valid": fingerprint.startswith(mark.hash)}


def _public_record(record: dict | None) -> dict | None:
    if not record:
        return None
    keys = ("source", "doi", "url", "type", "title", "authors", "year", "venue", "volume", "number", "pages", "publisher")
    return {k: record.get(k) for k in keys}


def run_dict(run: ReferenceRun | None, db: DBSession) -> dict | None:
    if run is None:
        return None
    _fail_if_orphaned(db, run)
    created_by = db.get(User, run.created_by) if run.created_by else None
    return {
        "id": run.id,
        "status": run.status,
        "claims": run.claims,
        "refresh": run.refresh,
        "total": run.total,
        "done": run.done,
        "error": run.error or None,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "created_by": serialize.person(created_by),
    }


def report(db: DBSession, project: Project) -> dict:
    """The report of §16, Routes."""
    return _build(db, project)[0]


def _build(db: DBSession, project: Project) -> tuple[dict, list[Entry]]:
    """The report and the parsed entries in the same order."""
    settings = get_settings()
    parsed = parse_project(project.id)
    today = _today()
    prints = [bibtex.fingerprint(e) for e in parsed.entries]
    rows = {}
    if prints:
        rows = {
            r.fingerprint: r
            for r in db.query(ReferenceResult).filter(
                ReferenceResult.project_id == project.id, ReferenceResult.fingerprint.in_(set(prints))
            )
        }

    cited: dict[str, list[latex.Cite]] = {}
    for c in parsed.cites:
        cited.setdefault(c.key, []).append(c)
    cited_lower = {k.lower() for k in cited}
    everything = "*" in cited
    key_count: dict[str, int] = {}
    doi_keys: dict[str, list[str]] = {}
    title_keys: dict[str, list[str]] = {}
    for e in parsed.entries:
        key_count[e.key.lower()] = key_count.get(e.key.lower(), 0) + 1
        doi = checks.identifiers(e).doi
        if doi:
            doi_keys.setdefault(doi, []).append(e.key)
        tk = _title_key(e)
        if tk:
            title_keys.setdefault(tk, []).append(e.key)

    entries = []
    for e, fp in zip(parsed.entries, prints, strict=True):
        findings = checks.offline(e, project.year, today)
        if key_count[e.key.lower()] > 1:
            findings.insert(0, checks.finding("duplicate_key", "error", f"Another entry has the key {e.key} as well."))
        doi = checks.identifiers(e).doi
        twins = [k for k in (doi_keys.get(doi, []) if doi else []) if k != e.key]
        if not twins:
            twins = [k for k in title_keys.get(_title_key(e), []) if k != e.key] if _title_key(e) else []
        twins = list(dict.fromkeys(twins))
        if twins:
            verb = "is" if len(twins) == 1 else "are"
            findings.append(checks.finding("duplicate", "warning", f"{_names(twins)} {verb} the same work, it seems."))
        uses = cited.get(e.key, []) or ([c for k, cs in cited.items() if k.lower() == e.key.lower() for c in cs])
        if not uses and not everything and e.key.lower() not in cited_lower:
            findings.append(checks.finding("not_cited", "info", "No \\cite uses this entry."))
        result = rows.get(fp)
        if result is not None:
            findings += list(result.findings or [])
            findings += list(result.claims or [])
        mark = _mark(e, fp)
        status = _status(findings, mark, result)
        entries.append({
            "key": e.key,
            "type": e.type,
            "path": e.path,
            "line": e.line,
            "end_line": e.end_line,
            "fingerprint": fp,
            "fields": e.values(),
            "status": status,
            "mark": mark,
            "record": _public_record(result.record if result else None),
            "checked_at": iso(result.checked_at) if result else None,
            "findings": findings,
            "claims_at": iso(result.claims_at) if result and result.claims_at else None,
            "cites": [{"path": c.path, "line": c.line} for c in uses],
        })

    keys_exact = {e.key for e in parsed.entries}
    keys_lower = {k.lower() for k in keys_exact}
    missing = []
    seen = set()
    for c in parsed.cites:
        if c.key == "*" or c.key in keys_exact or c.key.lower() in keys_lower:
            continue
        if (c.key, c.path, c.line) in seen:
            continue
        seen.add((c.key, c.path, c.line))
        missing.append({"key": c.key, "path": c.path, "line": c.line})

    counts = {s: 0 for s in STATUS_ORDER}
    for item in entries:
        counts[item["status"]] += 1
    broken = [{"path": p.path, "line": p.line, "key": p.key, "message": p.message} for p in parsed.broken]
    run = db.query(ReferenceRun).filter(ReferenceRun.project_id == project.id).order_by(ReferenceRun.started_at.desc()).first()
    data = {
        "run": run_dict(run, db),
        "claims_enabled": claims.enabled(),
        "network_enabled": settings.references_network,
        "files": parsed.files,
        "counts": counts,
        "problems": sum(counts[s] for s in PROBLEM_STATES) + len(broken) + len(missing),
        "missing": missing,
        "broken": broken,
        "entries": entries,
    }
    return data, parsed.entries


def _names(keys: list[str]) -> str:
    return keys[0] if len(keys) == 1 else ", ".join(keys[:-1]) + " and " + keys[-1]


def summary(db: DBSession, project: Project) -> dict:
    data = report(db, project)
    return {"problems": data["problems"], "total": len(data["entries"])}


def problems(db: DBSession, project: Project) -> list[dict]:
    """Errors and warnings in the shape of a compile's problems, for the
    editor. A checked entry shows only a retraction."""
    data, parsed = _build(db, project)
    out = []
    for b in data["broken"]:
        out.append({"level": "error", "path": b["path"], "line": b["line"], "message": b["message"]})
    for item, entry in zip(data["entries"], parsed, strict=True):
        for f in item["findings"]:
            if f["level"] not in ("error", "warning"):
                continue
            if item["status"] == "checked" and f["code"] != "retracted":
                continue
            if f["code"] == "claim":
                out.append({"level": "warning", "path": f.get("path"), "line": f.get("line"),
                            "message": f"{item['key']}: {f['message']}"})
                continue
            own = entry.field(f["field"]) if f.get("field") else None
            line = own.line if own is not None else item["line"]
            out.append({"level": f["level"], "path": item["path"], "line": line,
                        "message": f"{item['key']}: {f['message']}"})
        if item["status"] == "changed":
            out.append({"level": "warning", "path": item["path"], "line": item["line"],
                        "message": f"{item['key']} changed after it was marked as checked."})
    for m in data["missing"]:
        out.append({"level": "warning", "path": m["path"], "line": m["line"],
                    "message": f"There is no entry {m['key']} in the .bib files."})
    return out


# --- Runs ---------------------------------------------------------------------------

_guard = threading.Lock()
_active: dict[str, str] = {}  # project id -> run id


def _fail_if_orphaned(db: DBSession, run: ReferenceRun) -> None:
    if run.status != "running":
        return
    with _guard:
        alive = _active.get(run.project_id) == run.id
    if not alive:
        run.status = "failed"
        run.error = "The server restarted during the check."
        run.finished_at = utcnow()
        db.commit()


def latest_run(db: DBSession, project: Project) -> dict | None:
    run = db.query(ReferenceRun).filter(ReferenceRun.project_id == project.id).order_by(ReferenceRun.started_at.desc()).first()
    return run_dict(run, db)


def get_run(db: DBSession, project: Project, run_id: str) -> dict:
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise live.NotFound("There is no such check") from None
    run = db.get(ReferenceRun, run_id)
    if run is None or run.project_id != project.id:
        raise live.NotFound("There is no such check")
    return run_dict(run, db) or {}


def start(db: DBSession, project: Project, user: User, *, want_claims: bool, refresh: bool) -> tuple[dict, bool]:
    """Start a run, or return the one that is running. ``(run, started)``."""
    settings = get_settings()
    if not settings.references_network:
        raise Unavailable("Reference lookups are off on this server. REFERENCES_NETWORK is false.")
    if want_claims and not claims.enabled():
        raise Unavailable("The claim check is off on this server. ANTHROPIC_API_KEY is not set.")
    with _guard:
        running_id = _active.get(project.id)
        if running_id:
            run = db.get(ReferenceRun, running_id)
            if run is not None:
                return run_dict(run, db) or {}, False
        # Rows a restart cut off, which no thread works on.
        db.query(ReferenceRun).filter(ReferenceRun.project_id == project.id, ReferenceRun.status == "running").update(
            {"status": "failed", "error": "The server restarted during the check.", "finished_at": utcnow()}
        )
        run = ReferenceRun(id=str(uuid.uuid4()), project_id=project.id, status="running", claims=want_claims,
                           refresh=refresh, created_by=user.id)
        db.add(run)
        db.commit()
        _active[project.id] = run.id
    threading.Thread(target=_run, args=(project.id, run.id, want_claims, refresh), name="meertex-references",
                     daemon=True).start()
    return run_dict(run, db) or {}, True


def _bump(run_id: str, **values: object) -> None:
    with SessionLocal() as db:
        db.execute(update(ReferenceRun).where(ReferenceRun.id == run_id).values(**values))
        db.commit()


def _done_one(run_id: str) -> None:
    with SessionLocal() as db:
        db.execute(update(ReferenceRun).where(ReferenceRun.id == run_id).values(done=ReferenceRun.done + 1))
        db.commit()


def _store_lookup(project_id: str, fp: str, key: str, found: checks.Lookup) -> None:
    now = utcnow()
    stmt = insert(ReferenceResult).values(project_id=project_id, fingerprint=fp, key=key[:500], checked_at=now,
                                          record=found.record, findings=found.findings)
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "fingerprint"],
        set_={"key": key[:500], "checked_at": now, "record": found.record, "findings": found.findings},
    )
    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            return
        db.execute(stmt)
        db.commit()


def _store_claims(project_id: str, fp: str, key: str, found: list[dict], digest: str) -> None:
    now = utcnow()
    stmt = insert(ReferenceResult).values(project_id=project_id, fingerprint=fp, key=key[:500], checked_at=now,
                                          record=None, findings=[], claims=found, claims_hash=digest, claims_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "fingerprint"],
        set_={"claims": found, "claims_hash": digest, "claims_at": now},
    )
    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            return
        db.execute(stmt)
        db.commit()


# Tests replace this to answer without the network.
def make_sources(refresh: bool) -> sources.Sources:
    return sources.Sources(sources.Http(), sources.DbCache(refresh))


def _sentences(parsed: Parsed, keys: set[str]) -> list[claims.Sentence]:
    out: list[claims.Sentence] = []
    seen: set[tuple[str, int, str]] = set()
    for c in parsed.cites:
        if c.key not in keys or c.command.rstrip("*").lower() == "nocite":
            continue
        text = latex.sentence_at(parsed.tex.get(c.path, ""), c.start)
        if not text or (c.path, c.line, text) in seen:
            continue
        seen.add((c.path, c.line, text))
        out.append(claims.Sentence(c.path, c.line, text))
    return out


def _run(project_id: str, run_id: str, want_claims: bool, refresh: bool) -> None:
    src = None
    try:
        sources.prune_cache()
        parsed = parse_project(project_id)
        unique: dict[str, Entry] = {}
        for e in parsed.entries:
            unique.setdefault(bibtex.fingerprint(e), e)
        with SessionLocal() as db:
            rows = {
                r.fingerprint: r
                for r in db.query(ReferenceResult).filter(
                    ReferenceResult.project_id == project_id, ReferenceResult.fingerprint.in_(set(unique) or {""})
                )
            }
            fresh_after = utcnow() - RESULT_MAX_AGE
            todo = [
                (fp, e) for fp, e in unique.items()
                if refresh or fp not in rows or not rows[fp].checked_at or rows[fp].checked_at < fresh_after
                or (rows[fp].record is None and not rows[fp].findings)
            ]
        # Claims need a sentence, so \nocite does not count.
        cited_keys = {c.key for c in parsed.cites if c.command.rstrip("*").lower() != "nocite"}
        claim_todo = [(fp, e) for fp, e in unique.items() if want_claims and e.key in cited_keys]
        _bump(run_id, total=len(todo) + len(claim_todo))
        src = make_sources(refresh)

        def look(item: tuple[str, Entry]) -> None:
            fp, e = item
            try:
                found = checks.lookup(e, src)
            except Exception:
                logger.exception("Looking up %s failed", e.key)
                found = checks.Lookup(None, [checks.finding("unavailable", "info", "The lookup failed in meertex itself.")])
            _store_lookup(project_id, fp, e.key, found)
            _done_one(run_id)

        with ThreadPoolExecutor(LOOKUP_THREADS, thread_name_prefix="meertex-lookup") as pool:
            list(pool.map(look, todo))

        if claim_todo:
            with SessionLocal() as db:
                rows = {
                    r.fingerprint: r
                    for r in db.query(ReferenceResult).filter(
                        ReferenceResult.project_id == project_id,
                        ReferenceResult.fingerprint.in_({fp for fp, _ in claim_todo}),
                    )
                }
                records = {fp: r.record for fp, r in rows.items()}

            def judge(item: tuple[str, Entry]) -> None:
                fp, e = item
                record = records.get(fp)
                sentences = _sentences(parsed, {e.key})
                try:
                    found, digest = claims.check(e.key, fp, record, sentences, src, src.cache)
                except Exception:
                    logger.exception("The claim check of %s failed", e.key)
                    found, digest = [checks.finding("unavailable", "info", "The claim check failed in meertex itself.",
                                                    source="claude")], ""
                # Answers are cached by what was asked, so asking again is free.
                _store_claims(project_id, fp, e.key, found, digest)
                _done_one(run_id)

            with ThreadPoolExecutor(CLAIM_THREADS, thread_name_prefix="meertex-claims") as pool:
                list(pool.map(judge, claim_todo))
        _bump(run_id, status="done", finished_at=utcnow())
    except Exception as e:
        logger.exception("The reference check of project %s failed", project_id)
        _bump(run_id, status="failed", error=f"The check stopped ({e.__class__.__name__}).", finished_at=utcnow())
    finally:
        if src is not None:
            src.close()
        with _guard:
            if _active.get(project_id) == run_id:
                del _active[project_id]


def wait(project_id: str, timeout: float = 60.0) -> None:
    """Wait for the project's run to end. For tests."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _guard:
            if project_id not in _active:
                return
        time.sleep(0.02)
    raise TimeoutError("The reference check did not end")


# --- Writes -------------------------------------------------------------------------


def _find(doc, key: str) -> tuple[str, str, Entry]:
    """``(path, text, entry)`` of the one entry with ``key``. Raises
    live.NotFound and Ambiguous."""
    files = ydoc.materialize(doc)
    exact: list[tuple[str, str, Entry]] = []
    loose: list[tuple[str, str, Entry]] = []
    for path in sorted(files):
        f = files[path]
        if f.kind != "text" or not _is_bib(path):
            continue
        text = f.text or ""
        for e in bibtex.parse(text, path).entries:
            if e.key == key:
                exact.append((path, text, e))
            elif e.key.lower() == key.lower():
                loose.append((path, text, e))
    found = exact or loose
    if not found:
        raise live.NotFound(f"There is no entry {key}")
    if len(found) > 1:
        where = ", ".join(f"{p}:{e.line}" for p, _t, e in found)
        raise Ambiguous(f"Two entries have the key {key}: {where}")
    return found[0]


def _entry_out(db: DBSession, project: Project, key: str) -> dict:
    data = report(db, project)
    for item in data["entries"]:
        if item["key"] == key:
            return item
    for item in data["entries"]:
        if item["key"].lower() == key.lower():
            return item
    raise live.NotFound(f"There is no entry {key}")


def set_field(db: DBSession, project: Project, user: User, key: str, name: str, value: str) -> dict:
    name = name.strip().lower()
    if not _FIELD.match(name):
        raise live.Refused(f"{name!r} is not a field name")
    if name == MARK_FIELD:
        raise live.Refused("The checked mark is set with POST .../checked")
    if not bibtex.braces_balanced(value):
        raise live.Refused("The value's braces do not balance")
    with rooms.get(project.id).edit(user.email) as doc:
        path, text, entry = _find(doc, key)
        ydoc.put_text(doc, path, bibtex.set_field(text, entry, name, value))
    return _entry_out(db, project, key)


def _who(user: User) -> str:
    name = (user.name or "").strip() or user.email.split("@", 1)[0]
    return re.sub(r"[,{}\s]+", " ", name).strip()


def mark(db: DBSession, project: Project, user: User, key: str) -> dict:
    with rooms.get(project.id).edit(user.email) as doc:
        path, text, entry = _find(doc, key)
        value = bibtex.mark_value(_today(), _who(user), bibtex.fingerprint(entry))
        if entry.field(MARK_FIELD) is not None:
            text = bibtex.remove_field(text, entry, MARK_FIELD)
            entry = next(e for e in bibtex.parse(text, path).entries if e.start == entry.start)
        ydoc.put_text(doc, path, bibtex.set_field(text, entry, MARK_FIELD, value))
    return _entry_out(db, project, key)


def unmark(db: DBSession, project: Project, user: User, key: str) -> dict:
    with rooms.get(project.id).edit(user.email) as doc:
        path, text, entry = _find(doc, key)
        if entry.field(MARK_FIELD) is not None:
            ydoc.put_text(doc, path, bibtex.remove_field(text, entry, MARK_FIELD))
    return _entry_out(db, project, key)


# --- Adding an entry ----------------------------------------------------------------

_ARXIV_SOURCE = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.IGNORECASE)


def resolve(source: str, src: sources.Sources) -> dict:
    """The record a DOI, an arXiv id or link, an ISBN or a web page gives.
    Raises live.Refused for a source that is none of these, live.NotFound
    when it names nothing, and Unavailable when the service cannot be
    reached."""
    text = source.strip()
    if not text:
        raise live.Refused("Give a DOI, an arXiv id, an ISBN or a link")
    doi = sources.normalize_doi(text)
    arxiv = None
    m = _ARXIV_SOURCE.match(text)
    if m:
        arxiv = m.group(1)
    elif re.match(r"https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/", text, re.IGNORECASE):
        arxiv = checks.arxiv_in(text.split("/", 4)[-1])
    isbn = re.sub(r"[\s-]", "", text).upper()
    if doi and doi.startswith("10.48550/arxiv."):
        arxiv, doi = doi.split("arxiv.", 1)[1], None
    if arxiv:
        answer = src.doi(f"10.48550/arXiv.{arxiv}")
        if answer.ok and answer.record:
            return answer.record
        paper = src.arxiv_paper(arxiv)
        if paper.ok and paper.record:
            return paper.record
        if paper.ok or answer.ok:
            raise live.NotFound(f"arXiv has no paper {arxiv}")
        raise Unavailable(paper.error or answer.error)
    if doi:
        answer = src.doi(doi)
        if not answer.ok:
            raise Unavailable(answer.error)
        if answer.record is None:
            raise live.NotFound(f"doi.org does not know the DOI {doi}")
        return answer.record
    if re.fullmatch(r"\d{9}[\dX]|\d{13}", isbn):
        if not checks.isbn_ok(isbn):
            raise live.Refused(f"{text} is not a valid ISBN")
        answer = src.openlibrary_isbn(isbn)
        if not answer.ok:
            raise Unavailable(answer.error)
        if answer.record is None:
            raise live.NotFound(f"Open Library does not know the ISBN {isbn}")
        return answer.record
    if re.match(r"https?://", text, re.IGNORECASE):
        answer = src.page(text)
        info = answer.info
        if not answer.ok:
            raise live.Refused(info.get("detail") or "meertex does not fetch this link")
        status = info.get("status")
        if status in (404, 410) or info.get("error") in ("dns", "refused", "tls"):
            raise live.NotFound(f"The link is dead ({status or info.get('detail') or info.get('error')})")
        if status is None or not 200 <= status < 300:
            raise Unavailable(f"The site answered {status or info.get('error')}")
        record = info.get("record") or {}
        page_doi = sources.normalize_doi(record.get("doi") or "")
        if page_doi:
            answer = src.doi(page_doi)
            if answer.ok and answer.record:
                return answer.record
        if not record.get("title"):
            raise live.Refused("The page has no title to make an entry of")
        return {**record, "url": info.get("final_url") or text}
    raise live.Refused(f"{text} is no DOI, arXiv id, ISBN or link")


def add(
    db: DBSession,
    project: Project,
    user: User,
    source: str,
    key: str | None,
    path: str | None,
    dry_run: bool,
) -> dict:
    settings = get_settings()
    if not settings.references_network:
        raise Unavailable("Reference lookups are off on this server. REFERENCES_NETWORK is false.")
    if key is not None and not _KEY.match(key):
        raise live.Refused(f"{key!r} cannot be a BibTeX key")
    if path is not None:
        problem = ydoc.path_problem(path)
        if problem:
            raise live.Refused(problem)
        if not _is_bib(path):
            raise live.Refused("The file must be a .bib file")
    src = make_sources(False)
    try:
        record = resolve(source, src)
    finally:
        src.close()
    kind, fields = build.fields_from_record(record, _today())
    if not fields:
        raise live.Refused("The record has nothing to make an entry of")

    def compose(files: dict[str, ydoc.Entry]) -> tuple[str, str, str, int, str]:
        bibs = {p: (e.text or "") for p, e in files.items() if e.kind == "text" and _is_bib(p)}
        parsed = {p: bibtex.parse(t, p) for p, t in bibs.items()}
        taken = {e.key for f in parsed.values() for e in f.entries}
        if key is not None and key.lower() in {k.lower() for k in taken}:
            raise live.Refused(f"There is an entry {key} already")
        new_key = key or build.make_key(record, taken)
        target = path or (max(sorted(parsed), key=lambda p: len(parsed[p].entries)) if parsed else "refs.bib")
        current = bibs.get(target, "")
        if target not in bibs and target in files:
            raise live.Refused(f"{target} is not a text file")
        entry_text = bibtex.format_entry(kind, new_key, fields)
        base = current.rstrip("\n")
        new_text = (base + "\n\n" if base else "") + entry_text
        line = new_text[: len(new_text) - len(entry_text)].count("\n") + 1
        return new_key, target, new_text, line, entry_text

    if dry_run:
        with rooms.get(project.id).read() as doc:
            new_key, target, _text, line, entry_text = compose(ydoc.materialize(doc))
        return {"key": new_key, "path": target, "line": line, "bibtex": entry_text}
    with rooms.get(project.id).edit(user.email) as doc:
        new_key, target, new_text, line, entry_text = compose(ydoc.materialize(doc))
        ydoc.put_text(doc, target, new_text)
    return {"key": new_key, "path": target, "line": line, "bibtex": entry_text}
