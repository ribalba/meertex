"""Files of a project's live document and of its history (docs/DESIGN.md §11).

Every write here is a CRDT edit on the project's room, made in the request's
worker thread, so connected sync clients see it like any other edit. Reads of
``live`` come from the room, reads of any other ref from git.

What a browser does with a file is decided here, not by its name alone. Text
is always text/plain. A few image types and PDF are shown inline. Everything
else, SVG and HTML included, is a download. Every response carries ``nosniff``
and a sandbox CSP, so an uploaded file can never run script on our origin.
"""

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Annotated, TypeVar
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession
from starlette.background import BackgroundTask

from .. import live
from ..access import resolve_project
from ..config import get_settings
from ..database import DB
from ..models import Project, User
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api/projects", tags=["files"])

INLINE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
SAFE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}

T = TypeVar("T")


# --- Shared with blobs.py and history.py ------------------------------------------


def project_for(db: DBSession, user: User, ref: str, role: str) -> Project:
    project, _membership = resolve_project(db, user, ref, role)
    return project


def call_live(fn: Callable[..., T], *args: object) -> T:
    """Run a live function, with its NotFound as 404, Exists as 412 and
    Refused as 422."""
    try:
        return fn(*args)
    except live.NotFound as e:
        raise HTTPException(status_code=404, detail=e.args[0]) from None
    except live.Exists as e:
        raise HTTPException(status_code=412, detail=e.args[0]) from None
    except live.Refused as e:
        raise HTTPException(status_code=422, detail=e.args[0]) from None


def content_disposition(disposition: str, name: str) -> str:
    """``inline`` or ``attachment`` with a file name any browser reads, umlauts
    included (RFC 6266 with an ASCII fallback)."""
    encoded = quote(name, safe="")
    fallback = "".join(c if " " <= c <= "~" and c not in '"\\' else "_" for c in name)
    if encoded == name:
        return f'{disposition}; filename="{name}"'
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


async def read_body(request: Request) -> bytes:
    """The raw request body. Refused with 413 above MAX_BLOB_BYTES, before
    more than that is read."""
    limit = get_settings().max_blob_bytes
    too_large = HTTPException(status_code=413, detail=f"The body is larger than {limit} bytes")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


RawBody = Annotated[bytes, Depends(read_body)]


def _file_response(path: str, content: live.FileContent) -> Response:
    name = PurePosixPath(path).name
    if content.kind == "text":
        media, disposition = "text/plain; charset=utf-8", "inline"
    else:
        inline = INLINE_TYPES.get(PurePosixPath(name).suffix.lower())
        media = inline or "application/octet-stream"
        disposition = "inline" if inline else "attachment"
    headers = {**SAFE_HEADERS, "Content-Disposition": content_disposition(disposition, name)}
    if content.file is not None:
        return FileResponse(content.file, media_type=media, headers=headers)
    return Response(content.data, media_type=media, headers=headers)


# --- Files ---------------------------------------------------------------------------

Ref = Annotated[str, Query(description="live, HEAD, a version's tag or a commit")]


class MoveIn(BaseModel):
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)


@router.get("/{project}/files")
def list_files(project: str, user: SessionOrTokenUser, db: DB, ref: Ref = "live") -> list[dict]:
    p = project_for(db, user, project, "viewer")
    return call_live(live.list_files, p.id, ref)


# Only POST, so a file called "move" can still be read, written and deleted.
@router.post("/{project}/files/move")
def move_file(project: str, payload: MoveIn, user: SessionOrTokenUser, db: DB) -> list[dict]:
    """Rename a file, or a folder with everything in it. Returns the moved files."""
    p = project_for(db, user, project, "editor")
    return call_live(live.move_file, p.id, payload.from_, payload.to, user.email)


@router.get("/{project}/files/{path:path}")
def get_file(project: str, path: str, user: SessionOrTokenUser, db: DB, ref: Ref = "live") -> Response:
    p = project_for(db, user, project, "viewer")
    content = call_live(live.read_file, p.id, path, ref)
    if content is None:
        raise HTTPException(status_code=404, detail=f"There is no file {path}")
    return _file_response(path, content)


@router.put("/{project}/files/{path:path}")
def put_file(
    project: str,
    path: str,
    user: SessionOrTokenUser,
    db: DB,
    body: RawBody,
    if_none_match: Annotated[str | None, Header()] = None,
) -> dict:
    """Write the body to the live document: text as a diff against what is
    there, anything else as a blob. ``If-None-Match: *`` only creates: a file
    that is there already is left alone and the answer is 412."""
    p = project_for(db, user, project, "editor")
    create_only = (if_none_match or "").strip() == "*"
    return call_live(live.write_file, p.id, path, body, user.email, create_only)


@router.delete("/{project}/files/{path:path}")
def delete_file(project: str, path: str, user: SessionOrTokenUser, db: DB) -> dict:
    """Remove a file, or a folder with everything in it."""
    p = project_for(db, user, project, "editor")
    deleted = live.delete_file(p.id, path, user.email)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"There is no file or folder {path}")
    return {"deleted": deleted}


@router.get("/{project}/zip")
def download_zip(project: str, user: SessionOrTokenUser, db: DB, ref: Ref = "live") -> FileResponse:
    p = project_for(db, user, project, "viewer")
    fd, tmp = tempfile.mkstemp(prefix="meertex-", suffix=".zip")
    os.close(fd)
    try:
        call_live(live.write_zip, p.id, ref, Path(tmp))
    except BaseException:
        os.unlink(tmp)
        raise
    name = f"{p.name}-{re.sub(r'[^A-Za-z0-9._-]+', '-', ref)}.zip"
    headers = {**SAFE_HEADERS, "Content-Disposition": content_disposition("attachment", name)}
    return FileResponse(tmp, media_type="application/zip", headers=headers, background=BackgroundTask(os.unlink, tmp))


@router.get("/{project}/diff")
def diff(
    project: str,
    user: SessionOrTokenUser,
    db: DB,
    from_: Annotated[str, Query(alias="from")],
    to: Ref = "live",
    word: bool = False,
) -> dict:
    """``{from, to, files}`` with ``from`` and ``to`` as commits. ``live`` takes a
    snapshot first. ``word=true`` gives git's porcelain word diff."""
    p = project_for(db, user, project, "viewer")
    return call_live(live.diff, p.id, from_, to, word)
