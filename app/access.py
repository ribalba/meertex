"""Who may do what with a project (DESIGN §2).

Every route that takes a ``{ref}`` resolves it here. A project you are not a
member of is 404, never 403, so its existence does not leak. Too low a role for
a write is 403.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from .models import Membership, Project, User

ROLE_RANK = {"viewer": 0, "reviewer": 1, "editor": 2, "owner": 3}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def resolve_project(db: DBSession, user: User, ref: str, min_role: str = "viewer") -> tuple[Project, Membership]:
    """The project ``ref`` names (an id, or a name among the user's projects) and
    the user's membership of it. 404 when there is none, 409 when a name is
    ambiguous, 403 when the role is below ``min_role``."""
    rows: list[tuple[Project, Membership]] = []
    if _is_uuid(ref):
        rows = (
            db.query(Project, Membership)
            .join(Membership, Membership.project_id == Project.id)
            .filter(Project.id == ref, Membership.user_id == user.id)
            .all()
        )
    if not rows:
        rows = (
            db.query(Project, Membership)
            .join(Membership, Membership.project_id == Project.id)
            .filter(Project.name == ref, Membership.user_id == user.id)
            .all()
        )
    if not rows:
        raise HTTPException(status_code=404, detail="Project not found")
    if len(rows) > 1:
        # Two different owners used the same name, and you are in both.
        own = [r for r in rows if r[0].owner_id == user.id]
        if len(own) == 1:
            rows = own
        else:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"More than one project is called {ref}. Use its id.",
                    "candidates": [{"id": p.id, "name": p.name, "title": p.title} for p, _ in rows],
                },
            )
    project, membership = rows[0]
    if ROLE_RANK[membership.role] < ROLE_RANK[min_role]:
        raise HTTPException(status_code=403, detail=f"This needs the {min_role} role or higher")
    return project, membership


def can(membership: Membership, min_role: str) -> bool:
    return ROLE_RANK[membership.role] >= ROLE_RANK[min_role]
