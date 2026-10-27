"""Compiles: PDFs of a project's files, latexdiff PDFs between two refs, and
SyncTeX between a PDF and the sources (docs/DESIGN.md §14).

Viewers may compile, because reviewers read the PDF. A compile waits for TeX,
which takes seconds and, behind other compiles of the same project, minutes.
So the two routes that compile are async and run their work in threads of
their own (``_limiter``), and a crowd of them cannot take the threads that
every other route runs in.

The compile service is a separate container. When COMPILE_URL is empty or the
service cannot be reached, these routes answer 503 with the reason.
"""

import re
import uuid
from collections.abc import Callable
from functools import partial
from typing import TypeVar

import anyio
from anyio.lowlevel import RunVar
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from .. import compiler, live, serialize
from ..database import DB
from ..models import Compile, Project, User
from ..security import SessionOrTokenUser
from .files import SAFE_HEADERS, call_live, content_disposition, project_for

router = APIRouter(prefix="/api", tags=["compiles"])

T = TypeVar("T")

# Threads for compiles, per event loop (anyio keeps its own default limiter
# the same way).
COMPILE_THREADS = 16
_limiter_var: RunVar[anyio.CapacityLimiter] = RunVar("meertex_compile_limiter")


def _limiter() -> anyio.CapacityLimiter:
    try:
        return _limiter_var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(COMPILE_THREADS)
        _limiter_var.set(limiter)
        return limiter


async def _in_thread(fn: Callable[..., T], *args: object) -> T:
    return await anyio.to_thread.run_sync(partial(fn, *args), limiter=_limiter())


def _unavailable(e: compiler.Unavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=e.args[0])


def _find(db: DBSession, project: Project, compile_id: str) -> Compile:
    try:
        uuid.UUID(compile_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Compile not found") from None
    row = db.get(Compile, compile_id)
    if row is None or row.project_id != project.id:
        raise HTTPException(status_code=404, detail="Compile not found")
    return row


def _with_pdf(row: Compile) -> Compile:
    if not row.pdf_sha:
        raise HTTPException(status_code=404, detail="This compile made no PDF")
    return row


# --- Compiling ------------------------------------------------------------------------


class CompileIn(BaseModel):
    ref: str = Field(default="live", min_length=1, max_length=200)


class DiffIn(BaseModel):
    from_: str = Field(alias="from", min_length=1, max_length=200)
    to: str = Field(default="live", min_length=1, max_length=200)


def _compile(db: DBSession, user: User, project: str, ref: str) -> dict:
    p = project_for(db, user, project, "viewer")
    try:
        row = call_live(compiler.compile_ref, db, p, user.id, ref)
    except compiler.Unavailable as e:
        raise _unavailable(e) from None
    return serialize.compile_dict(row)


def _diff(db: DBSession, user: User, project: str, from_ref: str, to_ref: str) -> dict:
    p = project_for(db, user, project, "viewer")
    try:
        row = call_live(compiler.compile_diff, db, p, user.id, from_ref, to_ref)
    except compiler.Unavailable as e:
        raise _unavailable(e) from None
    return serialize.compile_dict(row)


@router.post("/projects/{project}/compile")
async def compile_project(project: str, user: SessionOrTokenUser, db: DB, body: CompileIn | None = None) -> dict:
    """Compile the files at ``ref`` (``live`` by default) and return the
    finished compile. The same files compiled before give that compile back."""
    return await _in_thread(_compile, db, user, project, body.ref if body else "live")


@router.post("/projects/{project}/pdfdiff")
async def pdfdiff(project: str, body: DiffIn, user: SessionOrTokenUser, db: DB) -> dict:
    """latexdiff from ``from`` to ``to``, compiled: a compile of kind pdfdiff."""
    return await _in_thread(_diff, db, user, project, body.from_, body.to)


# --- Reading compiles -----------------------------------------------------------------


@router.get("/projects/{project}/compiles/latest")
def latest_compile(project: str, user: SessionOrTokenUser, db: DB, ref: str = "live") -> dict | None:
    """The newest compile (kind pdf) of ``ref``, or null when there is none
    yet. That is no error, the editor asks every time it opens a paper. A tag
    or a commit finds every compile of its commit, under whichever name it was
    asked for. A ref that names nothing is 404."""
    p = project_for(db, user, project, "viewer")
    query = db.query(Compile).filter(Compile.project_id == p.id, Compile.kind == "pdf")
    if ref == "live":
        query = query.filter(Compile.commit.is_(None))
    else:
        call_live(live.check_ref, ref)
        commit = live.resolve_commit(p.id, ref)
        if commit is None:
            raise HTTPException(status_code=404, detail=f"Nothing is called {ref}")
        query = query.filter(Compile.commit == commit)
    row = query.order_by(Compile.created_at.desc()).first()
    return serialize.compile_dict(row) if row is not None else None


@router.get("/projects/{project}/compiles/{compile_id}")
def get_compile(project: str, compile_id: str, user: SessionOrTokenUser, db: DB) -> dict:
    p = project_for(db, user, project, "viewer")
    return serialize.compile_dict(_find(db, p, compile_id))


def _file_name(p: Project, row: Compile, suffix: str) -> str:
    ref = re.sub(r"[^A-Za-z0-9._-]+", "-", row.ref).strip("-") or "live"
    kind = "-diff" if row.kind == "pdfdiff" else ""
    return f"{p.name}{kind}-{ref}{suffix}"


@router.get("/projects/{project}/compiles/{compile_id}/pdf")
def compile_pdf(project: str, compile_id: str, user: SessionOrTokenUser, db: DB) -> FileResponse:
    p = project_for(db, user, project, "viewer")
    row = _with_pdf(_find(db, p, compile_id))
    path = live.blob_path(p.id, row.pdf_sha or "")
    if path is None:
        raise HTTPException(status_code=404, detail="This compile's PDF is missing")
    headers = {
        **SAFE_HEADERS,
        "Content-Disposition": content_disposition("inline", _file_name(p, row, ".pdf")),
        # A compile's PDF never changes, so the editor's reloads are free.
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    return FileResponse(path, media_type="application/pdf", headers=headers)


@router.get("/projects/{project}/compiles/{compile_id}/log")
def compile_log(project: str, compile_id: str, user: SessionOrTokenUser, db: DB) -> Response:
    p = project_for(db, user, project, "viewer")
    row = _find(db, p, compile_id)
    headers = {**SAFE_HEADERS, "Content-Disposition": content_disposition("inline", _file_name(p, row, ".log"))}
    return Response(row.log, media_type="text/plain; charset=utf-8", headers=headers)


# --- SyncTeX --------------------------------------------------------------------------


class EditIn(BaseModel):
    page: int = Field(ge=1, le=100_000)
    x: float = Field(ge=-100_000, le=100_000)
    y: float = Field(ge=-100_000, le=100_000)


class ViewIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    line: int = Field(ge=1, le=10_000_000)
    column: int = Field(default=0, ge=0, le=10_000_000)


def _synctex(fn: Callable[..., T], *args: object) -> T:
    try:
        return call_live(fn, *args)
    except compiler.Gone as e:
        raise HTTPException(status_code=410, detail=e.args[0]) from None
    except compiler.Unavailable as e:
        raise _unavailable(e) from None


@router.post("/projects/{project}/compiles/{compile_id}/synctex/edit")
def synctex_edit(project: str, compile_id: str, body: EditIn, user: SessionOrTokenUser, db: DB) -> dict:
    """The source line at a point of the PDF: ``{path, line, column}``. x and y
    are PDF points from the page's top left."""
    p = project_for(db, user, project, "viewer")
    row = _with_pdf(_find(db, p, compile_id))
    found = _synctex(compiler.synctex_edit, row, body.page, body.x, body.y)
    if found is None:
        raise HTTPException(status_code=404, detail="SyncTeX knows no source line at that point")
    return found


@router.post("/projects/{project}/compiles/{compile_id}/synctex/view")
def synctex_view(project: str, compile_id: str, body: ViewIn, user: SessionOrTokenUser, db: DB) -> dict:
    """Where a source line is in the PDF: ``{boxes: [{page, x, y, width, height}]}``."""
    p = project_for(db, user, project, "viewer")
    row = _with_pdf(_find(db, p, compile_id))
    return {"boxes": _synctex(compiler.synctex_view, row, body.path, body.line, body.column)}
