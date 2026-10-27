"""The reference check's routes (docs/DESIGN.md §16, Routes).

Viewers read the report, editors and the owner start runs and write into the
.bib files. Starting a run returns at once, the run goes on in a thread of
its own (app/references/service.py). Adding an entry asks a service, which
takes a few seconds, so that route runs in a worker thread like every plain
route.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..database import DB
from ..references import service
from ..security import SessionOrTokenUser
from .files import call_live, project_for

router = APIRouter(prefix="/api/projects", tags=["references"])


class CheckIn(BaseModel):
    claims: bool = False
    refresh: bool = False


class AddIn(BaseModel):
    source: str = Field(min_length=1, max_length=2000)
    key: str | None = Field(default=None, max_length=200)
    path: str | None = Field(default=None, max_length=1024)
    dry_run: bool = False


class FieldIn(BaseModel):
    field: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=20_000)


def _call(fn, *args):
    """A service call with its NotFound as 404, Refused as 422, Ambiguous as
    409 and Unavailable as 503."""

    def guarded():
        # Ambiguous is a Refused, so it is answered before call_live sees it.
        try:
            return fn(*args)
        except service.Ambiguous as e:
            raise HTTPException(status_code=409, detail=e.args[0]) from None

    try:
        return call_live(guarded)
    except service.Unavailable as e:
        raise HTTPException(status_code=503, detail=e.args[0]) from None


@router.get("/{project}/references")
def get_report(project: str, user: SessionOrTokenUser, db: DB) -> dict:
    p = project_for(db, user, project, "viewer")
    return service.report(db, p)


@router.get("/{project}/references/summary")
def get_summary(project: str, user: SessionOrTokenUser, db: DB) -> dict:
    p = project_for(db, user, project, "viewer")
    return service.summary(db, p)


@router.get("/{project}/references/problems")
def get_problems(project: str, user: SessionOrTokenUser, db: DB) -> list[dict]:
    p = project_for(db, user, project, "viewer")
    return service.problems(db, p)


@router.get("/{project}/references/check")
def latest_check(project: str, user: SessionOrTokenUser, db: DB) -> dict | None:
    p = project_for(db, user, project, "viewer")
    return service.latest_run(db, p)


@router.get("/{project}/references/check/{run_id}")
def get_check(project: str, run_id: str, user: SessionOrTokenUser, db: DB) -> dict:
    p = project_for(db, user, project, "viewer")
    return _call(service.get_run, db, p, run_id)


@router.post("/{project}/references/check")
def start_check(project: str, user: SessionOrTokenUser, db: DB, body: CheckIn | None = None) -> JSONResponse:
    """Start a run (202), or return the one that is running (200)."""
    p = project_for(db, user, project, "editor")
    body = body or CheckIn()
    run, started = _call(lambda: service.start(db, p, user, want_claims=body.claims, refresh=body.refresh))
    return JSONResponse(run, status_code=202 if started else 200)


@router.post("/{project}/references")
def add_reference(project: str, body: AddIn, user: SessionOrTokenUser, db: DB) -> JSONResponse:
    p = project_for(db, user, project, "editor")
    result = _call(service.add, db, p, user, body.source, body.key, body.path, body.dry_run)
    return JSONResponse(result, status_code=200 if body.dry_run else 201)


@router.post("/{project}/references/{key:path}/fields")
def set_field(project: str, key: str, body: FieldIn, user: SessionOrTokenUser, db: DB) -> dict:
    p = project_for(db, user, project, "editor")
    return _call(service.set_field, db, p, user, key, body.field, body.value)


@router.post("/{project}/references/{key:path}/checked")
def mark_checked(project: str, key: str, user: SessionOrTokenUser, db: DB) -> dict:
    p = project_for(db, user, project, "editor")
    return _call(service.mark, db, p, user, key)


@router.delete("/{project}/references/{key:path}/checked")
def unmark_checked(project: str, key: str, user: SessionOrTokenUser, db: DB) -> dict:
    p = project_for(db, user, project, "editor")
    return _call(service.unmark, db, p, user, key)
