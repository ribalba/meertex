"""Compiling a project's files (docs/DESIGN.md §14).

The server never runs TeX. It packs the files of a ref into a tar.gz, sends it
to the compile service (compile/) and keeps what comes back in a ``compiles``
row: the status, the end of the log, the problems found in it, and the PDF as
a project blob.

The editor asks for a compile at every pause in typing, so these rules keep
the work down:

* The files, the main file and the compiler are hashed. A finished compile
  with the same hash is reused instead of run again.
* One compile per project runs at a time. A second one waits for the
  project's lock, so one paper cannot take every TeX worker.
* A request for a hash that is being compiled waits for that compile and
  gets its result.
* A compile from an open editor runs in the paper's workspace on the compile
  service, which is sent only the files that changed and reuses the last
  build's files. A newer edit can stop it. Its hash differs from a clean
  compile's, so a version never gets a PDF that was built this way.
* Only the newest live PDFs of a paper are kept.

The second and third are held in this process, which is enough because the
server is one process (§1). A compile is synchronous for the request that asks
for it. Only a new version compiles in a thread of its own, started once the
version is committed, so making a version never waits for TeX.
"""

import gzip
import hashlib
import io
import logging
import tarfile
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace

import httpx
from sqlalchemy import event, or_
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
    """The compile service no longer has the job (it keeps the newest few),
    and its files cannot be compiled again because they have changed since."""


class Superseded(Exception):
    """A newer edit stopped this compile while it ran. ``args[0]`` says so, for a 409."""


class LiveFiles(Mapping[str, bytes]):
    """The files of the live document. The text is in memory. Blobs stay on
    disk until something reads them, because a paper's figures and data can be
    hundreds of MB and an editor's compile usually sends none of them."""

    def __init__(self, project_id: str, texts: dict[str, bytes], stored: dict[str, str]) -> None:
        self._project_id = project_id
        self._texts = texts
        self._stored = stored  # {path: the blob's SHA-256}

    def __getitem__(self, path: str) -> bytes:
        if path in self._texts:
            return self._texts[path]
        data = blobs.read(self._project_id, self._stored[path])
        if data is None:
            raise KeyError(path)
        return data

    def __iter__(self) -> Iterator[str]:
        yield from self._texts
        yield from self._stored

    def __len__(self) -> int:
        return len(self._texts) + len(self._stored)

    def digest(self, path: str) -> bytes:
        if path in self._texts:
            return hashlib.sha256(self._texts[path]).digest()
        return bytes.fromhex(self._stored[path])


def _digest(files: Mapping[str, bytes], path: str) -> bytes:
    """A file's SHA-256. A blob of the live document knows its own without being read."""
    return files.digest(path) if isinstance(files, LiveFiles) else hashlib.sha256(files[path]).digest()


@dataclass
class Source:
    """What one compile works on: the files of a ref, or two refs for a
    pdfdiff (``old`` are the files of ``from``). ``workspace`` names the
    compile service's folder for a compile from an open editor."""

    kind: str
    ref: str
    commit: str | None
    main_file: str
    compiler: str
    files: Mapping[str, bytes]
    old: Mapping[str, bytes] | None = None
    workspace: str | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        h = hashlib.sha256(_HASH_VERSION)
        for part in (self.kind, self.compiler, self.main_file):
            h.update(b"\0" + part.encode())
        if self.workspace is not None:
            # A PDF built on an earlier build's files is never handed out as a clean one.
            h.update(b"\0editor")
        for label, tree in (("new", self.files), ("old", self.old or {})):
            for path in sorted(tree):
                h.update(f"\0{label}\0{path}\0".encode())
                h.update(_digest(tree, path))
        self.content_hash = h.hexdigest()


def enabled() -> bool:
    return bool(get_settings().compile_url.strip())


def _require() -> None:
    if not enabled():
        raise Unavailable("Compiling is off on this server. COMPILE_URL is not set.")


# --- The files of a ref -------------------------------------------------------------


def files_at(project_id: str, ref: str) -> tuple[str | None, Mapping[str, bytes]]:
    """``(commit, {path: content})`` at ``ref``. ``live`` reads the room under
    its lock and the blob store, and has no commit. Anything else is read from
    git. Raises live.Refused and live.NotFound like the file routes."""
    live.check_ref(ref)
    if ref == "live":
        with rooms.get(project_id).read() as doc:
            entries = ydoc.materialize(doc)
        texts: dict[str, bytes] = {}
        stored: dict[str, str] = {}
        for path, entry in entries.items():
            if entry.kind == "text":
                texts[path] = (entry.text or "").encode()
            elif blobs.path(project_id, entry.sha or "") is not None:
                # Blobs are written once under their hash, so they are read
                # outside the room's lock. One whose bytes never came is left out.
                stored[path] = entry.sha or ""
        clashes = _clashes([*texts, *stored])
        texts = {path: data for path, data in texts.items() if path not in clashes}
        stored = {path: sha for path, sha in stored.items() if path not in clashes}
        return None, LiveFiles(project_id, texts, stored)
    commit = git.resolve(project_id, ref)
    if commit is None:
        raise live.NotFound(f"Nothing is called {ref}")
    tree = git.ls_tree(project_id, commit)
    contents = git.read_blobs(project_id, [f["sha"] for f in tree])
    return commit, {f["path"]: contents[f["sha"]] for f in tree if f["sha"] in contents}


def _clashes(paths: Iterable[str]) -> set[str]:
    """The paths that are a file and a folder at once, which cannot be
    unpacked. The folder wins, as in a snapshot (live.snapshot)."""
    names = set(paths)
    folders = {path.rsplit("/", i)[0] for path in names for i in range(1, path.count("/") + 1)}
    return names & folders


def pack(trees: Mapping[str, Mapping[str, bytes]]) -> bytes:
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


_GZIP = {"Content-Type": "application/gzip"}


def _post_job(source: Source, clean: bool) -> httpx.Response:
    """Hand ``source`` to the compile service. A workspace is told every
    file's hash first and says which files it lacks, so only those are sent."""
    params = {"main_file": source.main_file, "compiler": source.compiler}
    if source.kind == "pdfdiff":
        body = pack({"old/": source.old or {}, "new/": source.files})
        return _service("POST", "/latexdiff", params=params, content=body, headers=_GZIP)
    if source.workspace is None:
        return _service("POST", "/jobs", params=params, content=pack({"": source.files}), headers=_GZIP)
    manifest = {path: _digest(source.files, path).hex() for path in source.files}
    resp: httpx.Response | None = None
    # A 409 says the service lost its preparation (a restart). Once more is enough.
    for _attempt in range(2):
        plan = _service("POST", f"/workspaces/{source.workspace}/prepare", json={"files": manifest, "clean": clean})
        if plan.status_code != 200:
            return plan
        needed = plan.json()
        body = pack({"": {path: source.files[path] for path in needed["need"]}})
        job = {**params, "workspace": source.workspace, "token": needed["token"]}
        resp = _service("POST", "/jobs", params=job, content=body, headers=_GZIP)
        if resp.status_code != 409:
            break
    assert resp is not None
    return resp


def _run_service(source: Source, with_pdf: bool = True, clean: bool = False) -> _Result:
    """Send ``source`` to the compile service and fetch the PDF, unless
    ``with_pdf`` is false. Raises Unavailable."""
    started = time.monotonic()
    try:
        resp = _post_job(source, clean)
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
    if with_pdf and result.status in ("success", "errors") and result.job_id:
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
        self.error: Unavailable | Superseded | None = None


_guard = threading.Lock()
_flights: dict[tuple[str, str, bool], _Flight] = {}
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
            # An old live compile whose PDF was let go of cannot be handed out.
            or_(Compile.status == "failure", Compile.pdf_sha.isnot(None)),
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
        if source.kind == "pdf" and source.commit is None:
            _prune_live_pdfs(db, project_id)


def _prune_live_pdfs(db: DBSession, project_id: str) -> None:
    """Let go of the PDFs and logs of all but the newest live compiles. The
    rows stay, because the instance's activity counts them. A PDF goes from
    the blob store only when nothing else uses the same bytes: a version
    that adopted the compile, or a copy someone put into the paper."""
    old = (
        db.query(Compile)
        .filter(
            Compile.project_id == project_id,
            Compile.kind == "pdf",
            Compile.commit.is_(None),
            Compile.pdf_sha.isnot(None),
        )
        .order_by(Compile.created_at.desc(), Compile.id)
        .offset(get_settings().compile_keep_live_pdfs)
        .all()
    )
    if not old:
        return
    shas = {row.pdf_sha for row in old if row.pdf_sha}
    for row in old:
        row.pdf_sha = None
        row.log = ""
        row.problems = []
        row.job_id = None
    db.commit()
    used = {sha for (sha,) in db.query(Compile.pdf_sha).filter(Compile.project_id == project_id, Compile.pdf_sha.in_(shas))}
    used |= {sha for (sha,) in db.query(Version.pdf_sha).filter(Version.project_id == project_id, Version.pdf_sha.in_(shas))}
    free = shas - used
    if not free:
        return
    with rooms.get(project_id).read() as doc:
        free -= {entry.sha for entry in ydoc.materialize(doc).values() if entry.kind != "text"}
    for sha in free:
        blobs.delete(project_id, sha)


def _give_up(compile_id: str, error: Unavailable | Superseded, keep: bool) -> None:
    """The service could not compile, or a newer edit stopped it. A request's
    own row goes (the request answers 503 or 409), a version's row keeps the
    reason."""
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


def _compile_locked(project_id: str, source: Source, user_id: str | None, row_id: str | None, clean: bool) -> str:
    with _project_lock(project_id):
        with SessionLocal() as db:
            done = None if clean else _finished(db, project_id, source.content_hash)
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
            result = _run_service(source, clean=clean)
            if result.status == "cancelled":
                raise Superseded("A newer edit replaced this compile.")
            _store(project_id, row_id, source, result)
        except (Unavailable, Superseded) as e:
            _give_up(row_id, e, keep=not created)
            raise
        except BaseException as e:
            # Never leave a row "running" behind.
            _give_up(row_id, Unavailable(f"The compile could not run ({e.__class__.__name__})."), keep=not created)
            raise
        return row_id


def obtain(project_id: str, source: Source, user_id: str | None, row_id: str | None = None, clean: bool = False) -> str:
    """The id of a finished compile of ``source``: one that is reused, one that
    another request is making, or a new one. ``row_id`` names a running row
    to fill instead of making one. ``clean`` starts from nothing and reuses
    no finished compile. Raises Unavailable and Superseded."""
    key = (project_id, source.content_hash, clean)
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
        flight.compile_id = _compile_locked(project_id, source, user_id, row_id, clean)
        return flight.compile_id
    except (Unavailable, Superseded) as e:
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


def compile_ref(db: DBSession, project: Project, user_id: str, ref: str, editor: bool = False, clean: bool = False) -> Compile:
    """Compile the files at ``ref`` and return the finished row. ``editor``
    is a compile of the live text from an open editor, which runs in the
    paper's workspace and can be stopped by a newer edit. ``clean`` makes
    that one start from nothing. Raises Unavailable, Superseded, live.Refused
    and live.NotFound."""
    _require()
    project_id, main_file, compiler = project.id, project.main_file, project.compiler
    commit, files = files_at(project_id, ref)
    workspace = project_id if editor and ref == "live" else None
    source = Source("pdf", ref, commit, main_file, compiler, files, workspace=workspace)
    # Hand the request's database connection back while TeX runs.
    db.rollback()
    return _load(db, obtain(project_id, source, user_id, clean=clean and workspace is not None))


def cancel_editor_compile(project_id: str) -> bool:
    """Stop the editor compile that runs for this paper, if one does. Its
    request answers 409. Raises Unavailable."""
    _require()
    try:
        resp = _service("POST", f"/workspaces/{project_id}/cancel")
    except httpx.ReadTimeout as e:
        raise Unavailable("The compile service did not answer in time.") from e
    return resp.status_code == 200 and bool(resp.json().get("cancelled"))


def forget_workspace(project_id: str) -> None:
    """A deleted paper's files leave the compile service too. A service that
    cannot be reached does not keep the paper from being deleted. Its
    workspace then goes when newer ones push it out."""
    if not enabled():
        return
    try:
        _service("POST", f"/workspaces/{project_id}/forget")
    except (Unavailable, httpx.HTTPError) as e:
        logger.warning("Removing the workspace of project %s: %s", project_id, e)


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


def _ask_job(job_id: str | None, what: str, payload: dict) -> httpx.Response | None:
    """SyncTeX of one job, or None when the service no longer has it."""
    if not job_id:
        return None
    try:
        resp = _service("POST", f"/jobs/{job_id}/synctex/{what}", json=payload)
    except httpx.ReadTimeout as e:
        raise Unavailable("SyncTeX took too long.") from e
    return None if resp.status_code == 404 else resp


def _rerun(db: DBSession, row: Compile) -> str | None:
    """A new job for ``row``, whose job the service no longer has (it keeps
    the newest COMPILE_KEEP_JOBS). The same files and compiler lay out the
    same pages, so SyncTeX of the new job finds its places in the PDF ``row``
    already has. Every row of the same files gets the new job. None when the
    files cannot be had again: a live compile the text has moved on from, or
    a pdfdiff. Raises Unavailable."""
    if row.kind != "pdf":
        return None
    row_id, project_id, gone, content_hash = row.id, row.project_id, row.job_id, row.content_hash
    ref, commit, main_file, compiler = row.ref, row.commit, row.main_file, row.compiler
    # Hand the request's database connection back while TeX runs.
    db.rollback()
    with _project_lock(project_id):
        with SessionLocal() as s:
            now = s.get(Compile, row_id)
            if now is None:
                return None
            if now.job_id != gone:
                return now.job_id  # another request ran it meanwhile
        commit, files = files_at(project_id, commit or "live")
        source = Source("pdf", ref, commit, main_file, compiler, files)
        # A compile from the editor hashes differently. This one runs clean either way.
        same = source.content_hash == content_hash
        if not same and commit is None:
            same = replace(source, workspace=project_id).content_hash == content_hash
        if not same:
            return None
        result = _run_service(source, with_pdf=False)
        if result.status not in ("success", "errors") or not result.job_id:
            raise Unavailable(f"SyncTeX needed this PDF compiled again, which ended with {result.status}.")
        with SessionLocal() as s:
            s.query(Compile).filter(Compile.project_id == project_id, Compile.content_hash == content_hash).update(
                {Compile.job_id: result.job_id}, synchronize_session=False
            )
            s.commit()
        return result.job_id


def _synctex(db: DBSession, row: Compile, what: str, payload: dict) -> httpx.Response:
    _require()
    resp = _ask_job(row.job_id, what, payload)
    if resp is None:
        resp = _ask_job(_rerun(db, row), what, payload)
    if resp is None:
        raise Gone("The files have changed since this PDF was made. Compile again.")
    return resp


def synctex_edit(db: DBSession, row: Compile, page: int, x: float, y: float) -> dict | None:
    """``{path, line, column}`` of the source at a point of the PDF, or None."""
    resp = _synctex(db, row, "edit", {"page": page, "x": x, "y": y})
    if resp.status_code == 422:
        return None
    if resp.status_code != 200:
        raise Unavailable(f"SyncTeX answered {resp.status_code}: {_detail(resp)}")
    data = resp.json()
    return {"path": data["file"], "line": data["line"], "column": data["column"]}


def synctex_view(db: DBSession, row: Compile, path: str, line: int, column: int) -> list[dict]:
    """The boxes of the PDF that show a source line. Raises live.Refused for a
    path that is not a file of the compile."""
    resp = _synctex(db, row, "view", {"file": path, "line": line, "column": column})
    if resp.status_code == 422:
        raise live.Refused(_detail(resp))
    if resp.status_code != 200:
        raise Unavailable(f"SyncTeX answered {resp.status_code}: {_detail(resp)}")
    return list(resp.json())
