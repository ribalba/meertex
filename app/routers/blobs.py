"""Binary files by their SHA-256 (docs/DESIGN.md §8, §11).

A sync client uploads a blob before it writes the document entry that points
at it, and downloads one when an entry it does not have the bytes for
arrives. The URL names the content, so the server checks the hash: a blob
stored under the wrong name would be served for a file it is not.
"""

import hashlib

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import live
from ..database import DB
from ..live.blobs import SHA_RE
from ..security import SessionOrTokenUser
from .files import SAFE_HEADERS, RawBody, content_disposition, project_for

router = APIRouter(prefix="/api/projects", tags=["blobs"])


@router.put("/{project}/blobs/{sha}")
def put_blob(project: str, sha: str, user: SessionOrTokenUser, db: DB, body: RawBody) -> dict:
    p = project_for(db, user, project, "editor")
    if not SHA_RE.match(sha):
        raise HTTPException(status_code=422, detail="A blob is named by the SHA-256 of its content, in lowercase hex")
    actual = hashlib.sha256(body).hexdigest()
    if actual != sha:
        raise HTTPException(status_code=422, detail=f"The body's SHA-256 is {actual}, not {sha}")
    live.store_blob(p.id, body)
    return {"sha": sha, "size": len(body)}


@router.get("/{project}/blobs/{sha}")
def get_blob(project: str, sha: str, user: SessionOrTokenUser, db: DB) -> FileResponse:
    p = project_for(db, user, project, "viewer")
    stored = live.blob_path(p.id, sha)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"There is no blob {sha}")
    headers = {**SAFE_HEADERS, "Content-Disposition": content_disposition("attachment", sha)}
    return FileResponse(stored, media_type="application/octet-stream", headers=headers)
