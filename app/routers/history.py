"""A project's history: the snapshots of its live document (docs/DESIGN.md §9, §11)."""

from typing import Annotated

from fastapi import APIRouter, Query

from .. import live
from ..database import DB
from ..security import SessionOrTokenUser
from ..when import iso
from .files import project_for

router = APIRouter(prefix="/api/projects", tags=["history"])


@router.get("/{project}/history")
def history(
    project: str,
    user: SessionOrTokenUser,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[dict]:
    """``[{commit, parent, at, message, editors, files_changed}]``, newest first."""
    p = project_for(db, user, project, "viewer")
    return [{**c, "at": iso(c["at"])} for c in live.history(p.id, limit)]
