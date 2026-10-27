"""Compiling a project's files (docs/DESIGN.md §14).

The server never runs TeX. It packs the files of a ref into a tar.gz, sends it
to the compile service (compile/) and keeps what comes back in a ``compiles``
row: the status, the end of the log, the problems found in it, and the PDF as
a project blob.

The editor asks for a compile two seconds after every pause in typing, so
three rules keep the work down:

* The files, the main file and the compiler are hashed. A finished compile
  with the same hash is reused instead of run again.
* One compile per project runs at a time. A second one waits for the
  project's lock, so one paper cannot take every TeX worker.
* A request for a hash that is being compiled waits for that compile and
  gets its result.

The last two are held in this process, which is enough because the server is
one process (§1). A compile is synchronous for the request that asks for it.
Only a new version compiles in a thread of its own, started once the version
is committed, so making a version never waits for TeX.
"""

import gzip
import hashlib
import io
import logging
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy import event
from sqlalchemy.orm import Session as DBSession

from . import live, texlog
from .config import get_settings
from .database import SessionLocal
from .live import blobs, git, rooms, ydoc
from .models import Compile, Project, Version

logger = logging.getLogger("uvicorn.error")

# Results that the same files always give again. A timeout or an unreachable
# service may go away, so those compiles run again.
REUSABLE = ("success", "errors", "failure")
MAX_LOG_BYTES = 512 * 1024
_HASH_VERSION = b"meertex-compile-1"


class Unavailable(Exception):
    """Compiling is off (no COMPILE_URL), or the compile service cannot be
    reached or refused to work. ``args[0]`` says which, for a 503."""


class Gone(Exception):
    """The compile service no longer has the job (it keeps the newest few)."""


@dataclass
class Source:
    """What one compile works on: the files of a ref, or two refs for a
    pdfdiff (``old`` are the files of ``from``)."""

    kind: str
    ref: str
    commit: str | None
    main_file: str
    compiler: str
    files: dict[str, bytes]
    old: dict[str, bytes] | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        h = hashlib.sha256(_HASH_VERSION)
        for part in (self.kind, self.compiler, self.main_file):
            h.update(b"\0" + part.encode())
        for label, tree in (("new", self.files), ("old", self.old or {})):
            for path in sorted(tree):
                h.update(f"\0{label}\0{path}\0".encode())
                h.update(hashlib.sha256(tree[path]).digest())
        self.content_hash = h.hexdigest()


def enabled() -> bool:
    return bool(get_settings().compile_url.strip())


def _require() -> None:
    if not enabled():
        raise Unavailable("Compiling is off on this server. COMPILE_URL is not set.")


# --- The files of a ref -------------------------------------------------------------


def files_at(project_id: str, ref: str) -> tuple[str | None, dict[str, bytes]]:
    """``(commit, {path: content})`` at ``ref``. ``live`` reads the room under
    its lock and the blob store, and has no commit. Anything else is read from
    git. Raises live.Refused and live.NotFound like the file routes."""
    live.check_ref(ref)
    if ref == "live":
        with rooms.get(project_id).read() as doc:
            entries = ydoc.materialize(doc)
        files: dict[str, bytes] = {}
        for path, entry in entries.items():
            if entry.kind == "text":
                files[path] = (entry.text or "").encode()
            else:
                # Blobs are written once under their hash, so they are read
                # outside the room's lock. One whose bytes never came is left out.
                data = blobs.read(project_id, entry.sha or "")
                if data is not None:
                    files[path] = data
        return None, _without_clashes(files)
    commit = git.resolve(project_id, ref)
    if commit is None:
        raise live.NotFound(f"Nothing is called {ref}")
    tree = git.ls_tree(project_id, commit)
    contents = git.read_blobs(project_id, [f["sha"] for f in tree])
    return commit, {f["path"]: contents[f["sha"]] for f in tree if f["sha"] in contents}


def _without_clashes(files: dict[str, bytes]) -> dict[str, bytes]:
    """A path that is a file and a folder at once cannot be unpacked. The
    folder wins, as in a snapshot (live.snapshot)."""
    folders = {path.rsplit("/", i)[0] for path in files for i in range(1, path.count("/") + 1)}
    return {p: data for p, data in files.items() if p not in folders}


def pack(trees: dict[str, dict[str, bytes]]) -> bytes:
    """A tar.gz of ``{prefix: {path: content}}``. The same files give the same bytes."""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=5) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for prefix, files in trees.items():
            for path in sorted(files):
                data = files[path]
                info = tarfile.TarInfo(prefix + path)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- The compile service ------------------------------------------------------------


def _service(method: str, path: str, **kwargs: object) -> httpx.Response:
    """A request to the compile service. Raises Unavailable when it cannot be
    reached, refuses the token or is busy. A read that takes too long raises
    httpx.ReadTimeout."""
    settings = get_settings()
    base = settings.compile_url.strip().rstrip("/")
    # Room for the service's queue, latexdiff and the compile itself.
    timeout = httpx.Timeout(2 * settings.compile_timeout_seconds + 60, connect=10)
    try:
        # A proxy configured for the outside world must not carry the token
        # to the compile service (trust_env=False).
        headers = {"Authorization": f"Bearer {settings.compile_token}"}
        with httpx.Client(timeout=timeout, headers=headers, trust_env=False) as client:
            resp = client.request(method, base + path, **kwargs)
    except httpx.ReadTimeout:
        raise
    except httpx.HTTPError as e:
        raise Unavailable(f"The compile service at {base} cannot be reached ({e.__class__.__name__}: {e}).") from e
    if resp.status_code == 401:
        raise Unavailable("The compile service refused the token. COMPILE_TOKEN must be the same for web and compile.")
    if resp.status_code >= 500:
        raise Unavailable(f"The compile service answered {resp.status_code}: {_detail(resp)}")
    return resp


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail") or resp.text)
    except ValueError:
        return resp.text.strip()[:500]


def _trim(log: str) -> str:
    """The last MAX_LOG_BYTES of the log, cut at a line start."""
    data = log.encode()
    if len(data) <= MAX_LOG_BYTES:
        return log
    tail = data[-MAX_LOG_BYTES:].decode(errors="ignore")
    return tail[tail.find("\n") + 1 :] if "\n" in tail else tail


@dataclass
class _Result:
    status: str
    log: str
    duration_ms: int | None = None
    job_id: str | None = None
    pdf: bytes | None = None


def _run_service(source: Source) -> _Result:
    """Send ``source`` to the compile service and fetch the PDF. Raises Unavailable."""
    if source.kind == "pdfdiff":
        path, body = "/latexdiff", pack({"old/": source.old or {}, "new/": source.files})
    else:
        path, body = "/jobs", pack({"": source.files})
    params = {"main_file": source.main_file, "compiler": source.compiler}
    started = time.monotonic()
    try:
        resp = _service("POST", path, params=params, content=body, headers={"Content-Type": "application/gzip"})
    except httpx.ReadTimeout:
        waited = int(time.monotonic() - started)
        return _Result("timeout", f"The compile service did not answer within {waited} seconds.\n")
    if resp.status_code in (400, 413):
        # The upload was refused (too large, a path it does not take). The
        # same files are refused again, so this is a failure, not an outage.
        return _Result("failure", f"The compile service refused the files: {_detail(resp)}\n")
    if resp.status_code != 200:
        raise Unavailable(f"The compile service answered {resp.status_code}: {_detail(resp)}")
    data = resp.json()
    result = _Result(
        status=str(data.get("status") or "failure"),
        log=str(data.get("log") or ""),
        duration_ms=data.get("duration_ms"),
        job_id=data.get("job_id"),
    )
    if result.status in ("success", "errors") and result.job_id:
        try:
            pdf = _service("GET", f"/jobs/{result.job_id}/output.pdf")
        except httpx.ReadTimeout as e:
            raise Unavailable("The compile service did not send the PDF in time.") from e
        if pdf.status_code != 200 or not pdf.content.startswith(b"%PDF-"):
            raise Unavailable(f"The compile service did not send the PDF ({pdf.status_code}).")
        result.pdf = pdf.content
    return result


# --- Compiles, one at a time per project --------------------------------------------


class _Flight:
    """A compile of one hash that is under way, for whoever asks for the same."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.compile_id: str | None = None
        self.error: Unavailable | None = None


_guard = threading.Lock()
_flights: dict[tuple[str, str], _Flight] = {}
_project_locks: dict[str, threading.Lock] = {}


def _project_lock(project_id: str) -> threading.Lock:
    with _guard:
        return _project_locks.setdefault(project_id, threading.Lock())


def _new_row(project_id: str, source: Source, user_id: str | None, status: str = "running") -> Compile:
    return Compile(
        id=str(uuid.uuid4()),
        project_id=project_id,
        kind=source.kind,
        ref=source.ref,
        commit=source.commit,
        content_hash=source.content_hash,
        compiler=source.compiler,
        main_file=source.main_file,
        status=status,
        log="",
        problems=[],
        created_by=user_id,
    )


def _finished(db: DBSession, project_id: str, content_hash: str) -> Compile | None:
    return (
        db.query(Compile)
        .filter(
            Compile.project_id == project_id,
            Compile.content_hash == content_hash,
            Compile.status.in_(REUSABLE),
        )
        .order_by(Compile.created_at.desc())
        .first()
    )


def _adopt(db: DBSession, done: Compile, project_id: str, source: Source, user_id: str | None, row_id: str | None) -> str:
    """Hand out the finished compile ``done`` for ``source``. A request for the
    same ref gets the same row. Another ref (a tag whose files are the live
    ones, say) gets a row of its own with the same result, so its ``ref`` and
    ``commit`` are right and ``latest`` finds it. ``row_id`` is a running row
    that waits for a result (a version's)."""
    if row_id is None and (done.kind, done.ref, done.commit) == (source.kind, source.ref, source.commit):
        return done.id
    row = db.get(Compile, row_id) if row_id else None
    if row_id and row is None:
        return row_id  # the project went away meanwhile
    if row is None:
        row = _new_row(project_id, source, user_id)
        db.add(row)
    row.content_hash = source.content_hash
    row.status = done.status
    row.duration_ms = done.duration_ms
    row.log = done.log
    row.problems = list(done.problems or [])
    row.pdf_sha = done.pdf_sha
    row.job_id = done.job_id
    db.commit()
    return row.id


def _store(project_id: str, compile_id: str, source: Source, result: _Result) -> None:
    files = set(source.files)
    problems = texlog.parse(result.log, source.main_file, files)
    with SessionLocal() as db:
        row = db.get(Compile, compile_id)
        if row is None or db.get(Project, project_id) is None:
            return  # deleted while TeX ran
        row.pdf_sha = live.store_blob(project_id, result.pdf) if result.pdf else None
        row.status = result.status
        row.duration_ms = result.duration_ms
        row.log = _trim(result.log)
        row.problems = problems
        row.job_id = result.job_id
        db.commit()


def _give_up(compile_id: str, error: Unavailable, keep: bool) -> None:
    """The service could not compile. A request's own row goes (the request
    answers 503), a version's row keeps the reason."""
    with SessionLocal() as db:
        row = db.get(Compile, compile_id)
        if row is None:
            return
        if keep:
            row.status = "unavailable"
            row.log = f"{error.args[0]}\n"
        else:
            db.delete(row)
        db.commit()


def _compile_locked(project_id: str, source: Source, user_id: str | None, row_id: str | None) -> str:
    with _project_lock(project_id):
        with SessionLocal() as db:
            done = _finished(db, project_id, source.content_hash)
            if done is not None:
                return _adopt(db, done, project_id, source, user_id, row_id)
            created = row_id is None
            if created:
                row = _new_row(project_id, source, user_id)
                db.add(row)
                db.commit()
                row_id = row.id
        assert row_id is not None
        try:
            result = _run_service(source)
            _store(project_id, row_id, source, result)
        except Unavailable as e:
            _give_up(row_id, e, keep=not created)
            raise
        except BaseException as e:
            # Never leave a row "running" behind.
            _give_up(row_id, Unavailable(f"The compile could not run ({e.__class__.__name__})."), keep=not created)
            raise
        return row_id


def obtain(project_id: str, source: Source, user_id: str | None, row_id: str | None = None) -> str:
    """The id of a finished compile of ``source``: one that is reused, one that
    another request is making, or a new one. ``row_id`` names a running row
    to fill instead of making one. Raises Unavailable."""
    key = (project_id, source.content_hash)
    while True:
        with _guard:
            flight = _flights.get(key)
            if flight is None:
                flight = _flights[key] = _Flight()
                break
        flight.done.wait()
        if flight.error is not None:
            if row_id is not None:
                _give_up(row_id, flight.error, keep=True)
            raise flight.error
        if flight.compile_id is not None:
            with SessionLocal() as db:
                done = db.get(Compile, flight.compile_id)
                if done is not None:
                    return _adopt(db, done, project_id, source, user_id, row_id)
        # The other compile ended without a result (an unexpected error, or
        # its project row went). Try again.
    try:
        flight.compile_id = _compile_locked(project_id, source, user_id, row_id)
        return flight.compile_id
    except Unavailable as e:
        flight.error = e
        raise
    finally:
        with _guard:
            if _flights.get(key) is flight:
                del _flights[key]
        flight.done.set()


def _load(db: DBSession, compile_id: str) -> Compile:
    row = db.get(Compile, compile_id, populate_existing=True)
    if row is None:  # deleted with its project meanwhile
        raise live.NotFound("The compile is gone")
    return row


def compile_ref(db: DBSession, project: Project, user_id: str, ref: str) -> Compile:
    """Compile the files at ``ref`` and return the finished row. Raises
    Unavailable, live.Refused and live.NotFound."""
    _require()
    project_id, main_file, compiler = project.id, project.main_file, project.compiler
    commit, files = files_at(project_id, ref)
    source = Source("pdf", ref, commit, main_file, compiler, files)
    # Hand the request's database connection back while TeX runs.
    db.rollback()
    return _load(db, obtain(project_id, source, user_id))


def compile_diff(db: DBSession, project: Project, user_id: str, from_ref: str, to_ref: str) -> Compile:
    """latexdiff between two refs, compiled. Cached like any other compile."""
    _require()
    project_id, main_file, compiler = project.id, project.main_file, project.compiler
    old_commit, old = files_at(project_id, from_ref)
    commit, files = files_at(project_id, to_ref)
    ref = f"{from_ref}..{to_ref}"
    if len(ref) > 300:
        ref = f"{(old_commit or 'live')[:12]}..{(commit or 'live')[:12]}"
    source = Source("pdfdiff", ref, commit, main_file, compiler, files, old)
    db.rollback()
    return _load(db, obtain(project_id, source, user_id))


# --- Versions -------------------------------------------------------------------------

_PENDING = "meertex_compiles_after_commit"


def compile_version_later(db: DBSession, project: Project, version: Version) -> None:
    """Give a new version a running compile of its commit, and start it once
    the version is committed. Called inside the transaction that makes the
    version. A rolled back version starts nothing."""
    if not enabled():
        return
    row = Compile(
        id=str(uuid.uuid4()),
        project_id=project.id,
        kind="pdf",
        ref=version.tag,
        commit=version.commit,
        # Hashing reads every file, which the thread does, not the request.
        content_hash="",
        compiler=project.compiler,
        main_file=project.main_file,
        status="running",
        log="",
        problems=[],
        created_by=version.created_by,
    )
    db.add(row)
    version.compile_id = row.id
    if _PENDING not in db.info:
        db.info[_PENDING] = []
        event.listen(db, "after_commit", _start_pending)
        event.listen(db, "after_rollback", _drop_pending)
    job = (project.id, row.id, version.tag, version.commit, project.main_file, project.compiler, version.created_by)
    db.info[_PENDING].append(job)


def _start_pending(db: DBSession) -> None:
    for job in db.info.get(_PENDING, []):
        threading.Thread(target=_compile_version, args=job, name="meertex-compile", daemon=True).start()
    db.info[_PENDING] = []


def _drop_pending(db: DBSession) -> None:
    db.info[_PENDING] = []


def _compile_version(
    project_id: str, compile_id: str, tag: str, commit: str, main_file: str, compiler: str, user_id: str | None
) -> None:
    try:
        _commit, files = files_at(project_id, commit)
        source = Source("pdf", tag, commit, main_file, compiler, files)
        with SessionLocal() as db:
            row = db.get(Compile, compile_id)
            if row is None:
                return
            row.content_hash = source.content_hash
            db.commit()
        obtain(project_id, source, user_id, row_id=compile_id)
    except Unavailable as e:
        logger.warning("Compiling version %s of project %s: %s", tag, project_id, e)
    except Exception as e:
        logger.exception("Compiling version %s of project %s failed", tag, project_id)
        _give_up(compile_id, Unavailable(f"The compile could not run ({e.__class__.__name__})."), keep=True)


# --- SyncTeX ----------------------------------------------------------------------------


def _synctex(row: Compile, what: str, payload: dict) -> httpx.Response:
    _require()
    if not row.job_id:
        raise Gone("The compile service no longer has this compile. Compile again.")
    try:
        resp = _service("POST", f"/jobs/{row.job_id}/synctex/{what}", json=payload)
    except httpx.ReadTimeout as e:
        raise Unavailable("SyncTeX took too long.") from e
    if resp.status_code == 404:
        raise Gone("The compile service no longer has this compile. Compile again.")
    return resp


def synctex_edit(row: Compile, page: int, x: float, y: float) -> dict | None:
    """``{path, line, column}`` of the source at a point of the PDF, or None."""
    resp = _synctex(row, "edit", {"page": page, "x": x, "y": y})
    if resp.status_code == 422:
        return None
    if resp.status_code != 200:
        raise Unavailable(f"SyncTeX answered {resp.status_code}: {_detail(resp)}")
    data = resp.json()
    return {"path": data["file"], "line": data["line"], "column": data["column"]}


def synctex_view(row: Compile, path: str, line: int, column: int) -> list[dict]:
    """The boxes of the PDF that show a source line. Raises live.Refused for a
    path that is not a file of the compile."""
    resp = _synctex(row, "view", {"file": path, "line": line, "column": column})
    if resp.status_code == 422:
        raise live.Refused(_detail(resp))
    if resp.status_code != 200:
        raise Unavailable(f"SyncTeX answered {resp.status_code}: {_detail(resp)}")
    return list(resp.json())
