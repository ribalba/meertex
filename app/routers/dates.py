"""A project's dates and answering their questions (DESIGN §5, §6, §11).

Adding, changing and removing a date returns the date. Answering and taking
an answer back return the whole project, because an answer can change the
outcome, the versions and the next date at once, and the client redraws all
of it.
"""

from fastapi import APIRouter

from .. import serialize, services
from ..access import resolve_project
from ..database import DB
from ..models import utcnow
from ..project_schemas import AnswerIn, DateIn, DatePatch
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api", tags=["dates"])


@router.post("/projects/{ref}/dates", status_code=201)
def add_date(ref: str, body: DateIn, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref, "editor")
    d = services.new_date(project.id, body, services.next_date_position(db, project.id))
    db.add(d)
    project.updated_at = utcnow()
    db.commit()
    return serialize.date_dict(d, utcnow())


@router.patch("/projects/{ref}/dates/{date_id}")
def update_date(ref: str, date_id: str, body: DatePatch, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref, "editor")
    d = services.find_date(db, project, date_id)
    services.update_date(d, body, user)
    project.updated_at = utcnow()
    db.commit()
    return serialize.date_dict(d, utcnow())


@router.delete("/projects/{ref}/dates/{date_id}")
def delete_date(ref: str, date_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref, "editor")
    d = services.find_date(db, project, date_id)
    db.delete(d)
    project.updated_at = utcnow()
    db.commit()
    return {"ok": True}


@router.post("/projects/{ref}/dates/{date_id}/answer")
def answer(ref: str, date_id: str, body: AnswerIn, db: DB, user: SessionOrTokenUser) -> dict:
    project, membership = resolve_project(db, user, ref, "editor")
    d = services.find_date(db, project, date_id)
    services.answer_date(db, project, d, body, user)
    db.commit()
    return services.detail(db, user, project, membership)


@router.delete("/projects/{ref}/dates/{date_id}/answer")
def clear_answer(ref: str, date_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    project, membership = resolve_project(db, user, ref, "editor")
    d = services.find_date(db, project, date_id)
    services.clear_answer(project, d)
    db.commit()
    return services.detail(db, user, project, membership)
