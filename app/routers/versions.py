"""Versions, the fixed and named states of a project's files (DESIGN §7, §11).

A version is a git tag plus a row. Its kind, tag and commit never change once
made, only its label and note do. The PDF that was really handed in can be
attached to it and is stored as a project blob, like any binary file.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from .. import live, serialize, services
from ..access import resolve_project
from ..config import get_settings
from ..database import DB
from ..models import Project, User, Version, utcnow
from ..project_schemas import VersionCreate, VersionPatch
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api", tags=["versions"])


def _out(db: DB, version: Version) -> dict:
    creator = db.get(User, version.created_by) if version.created_by else None
    return serialize.version_dict(version, creator)


@router.get("/projects/{ref}/versions")
def list_versions(ref: str, db: DB, user: SessionOrTokenUser) -> list[dict]:
    project, _ = resolve_project(db, user, ref)
    versions = db.query(Version).filter(Version.project_id == project.id).order_by(Version.number).all()
    ids = {v.created_by for v in versions if v.created_by}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(ids)).all()} if ids else {}
    return [serialize.version_dict(v, users.get(v.created_by or "")) for v in versions]


@router.post("/projects/{ref}/versions", status_code=201)
def create_version(ref: str, body: VersionCreate, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref, "editor")
    date = services.find_date(db, project, body.date_id) if body.date_id else None
    version = services.create_version(
        db, project, user, body.kind, label=body.label, note=body.note, date=date, at=body.at or "now"
    )
    db.commit()
    return _out(db, version)


@router.get("/projects/{ref}/versions/{vref}")
def get_version(ref: str, vref: str, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref)
    version = services.find_version(db, project, vref)
    out = _out(db, version)
    try:
        out["files"] = live.list_files(project.id, version.commit)
    except KeyError:
        out["files"] = []
    return out


@router.patch("/projects/{ref}/versions/{vref}")
def update_version(ref: str, vref: str, body: VersionPatch, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref, "editor")
    version = services.find_version(db, project, vref)
    data = body.model_dump(exclude_unset=True)
    if "label" in data:
        label = (data["label"] or "").strip()
        if not label:
            raise services.unprocessable("A version needs a label")
        version.label = label
    if "note" in data:
        version.note = data["note"] or ""
    db.commit()
    return _out(db, version)


@router.delete("/projects/{ref}/versions/{vref}")
def delete_version(ref: str, vref: str, db: DB, user: SessionOrTokenUser) -> dict:
    """The tag goes, the commit stays in the history."""
    project, _ = resolve_project(db, user, ref, "owner")
    version = services.find_version(db, project, vref)
    tag = version.tag
    db.delete(version)
    project.updated_at = utcnow()
    db.commit()
    live.delete_tag(project.id, tag)
    return {"ok": True}


async def _read_pdf(request: Request) -> bytes:
    """The body, at most MAX_BLOB_BYTES, and it must look like a PDF."""
    limit = get_settings().max_blob_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"The PDF is larger than {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"The PDF is larger than {limit} bytes")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="That is not a PDF. A PDF starts with %PDF-.")
    return data


def _pdf_name(name: str | None, project: Project, version: Version) -> str:
    """A file name for downloads: no folders, no control characters."""
    cleaned = "".join(ch for ch in (name or "") if ch.isprintable() and ch not in '/\\"').strip()
    return cleaned[:300] or f"{project.name}-{version.tag}.pdf"


def _attach_pdf(db: DB, user: User, ref: str, vref: str, data: bytes, name: str | None) -> dict:
    project, _ = resolve_project(db, user, ref, "editor")
    version = services.find_version(db, project, vref)
    version.pdf_sha = live.store_blob(project.id, data)
    version.pdf_name = _pdf_name(name, project, version)
    db.commit()
    return _out(db, version)


@router.put("/projects/{ref}/versions/{vref}/pdf")
async def put_pdf(
    ref: str, vref: str, request: Request, db: DB, user: SessionOrTokenUser, name: str | None = None
) -> dict:
    # Check the caller's access before taking a 50 MB body.
    await run_in_threadpool(resolve_project, db, user, ref, "editor")
    data = await _read_pdf(request)
    return await run_in_threadpool(_attach_pdf, db, user, ref, vref, data, name)


@router.get("/projects/{ref}/versions/{vref}/pdf")
def get_pdf(ref: str, vref: str, db: DB, user: SessionOrTokenUser) -> FileResponse:
    project, _ = resolve_project(db, user, ref)
    version = services.find_version(db, project, vref)
    path = live.blob_path(project.id, version.pdf_sha) if version.pdf_sha else None
    if path is None:
        raise HTTPException(status_code=404, detail="No PDF is attached to this version")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=version.pdf_name or f"{project.name}-{version.tag}.pdf",
        content_disposition_type="inline",
    )
