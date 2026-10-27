"""Every waiting question across your projects (DESIGN §6, §11).

Only projects you can answer for (editor or owner) and that are not archived
count. A viewer cannot answer, and an archived paper should stop asking.
"""

from fastapi import APIRouter

from .. import lifecycle, services
from ..database import DB
from ..models import Membership, Project, utcnow
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api", tags=["questions"])


@router.get("/questions")
def list_questions(db: DB, user: SessionOrTokenUser) -> list[dict]:
    projects = (
        db.query(Project)
        .join(Membership, Membership.project_id == Project.id)
        .filter(
            Membership.user_id == user.id,
            Membership.role.in_(("owner", "editor")),
            Project.archived.is_(False),
        )
        .all()
    )
    ids = [p.id for p in projects]
    dates = services.dates_by_project(db, ids)
    submitted = services.submitted_projects(db, ids)
    now = utcnow()
    out: list[dict] = []
    for p in projects:
        out.extend(lifecycle.questions(p, dates[p.id], p.id in submitted, now))
    out.sort(key=lambda q: (q["due_at"], q["project_name"]))
    return out
