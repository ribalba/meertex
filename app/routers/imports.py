"""Importing a template zip into a project (docs/DESIGN.md §4, §11)."""

from fastapi import APIRouter, HTTPException

from .. import importer, live
from ..database import DB
from ..models import utcnow
from ..security import SessionOrTokenUser
from .files import RawBody, project_for

router = APIRouter(prefix="/api/projects", tags=["imports"])


@router.post("/{project}/import")
def import_zip(project: str, user: SessionOrTokenUser, db: DB, body: RawBody, replace: bool = False) -> dict:
    """Write the files of a zip (a publisher's author kit) into the live
    document. When the project's main file is not among its files afterwards,
    the main file becomes the zip's most likely paper."""
    p = project_for(db, user, project, "editor")
    try:
        result = importer.import_zip(p.id, body, user.email, replace=replace)
    except importer.ImportRefused as exc:
        raise HTTPException(status_code=422, detail=exc.args[0]) from None

    present = {f["path"] for f in live.list_files(p.id)}
    main_changed = bool(result.main_file) and p.main_file not in present
    if main_changed:
        p.main_file = result.main_file
    p.updated_at = utcnow()
    db.commit()
    return {
        "files": result.files,
        "skipped": result.skipped,
        "main_file": p.main_file,
        "main_file_changed": main_changed,
        "main_candidates": result.main_candidates,
    }
