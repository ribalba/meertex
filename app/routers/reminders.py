"""Reminder mails before deadlines: the preview and muting a paper
(docs/DESIGN.md §5, Reminders). The settings are on PATCH /api/auth/me."""

from fastapi import APIRouter

from .. import reminders, services
from ..access import resolve_project
from ..database import DB
from ..models import utcnow
from ..project_schemas import RemindersIn
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api", tags=["reminders"])


@router.get("/reminders/preview")
def preview(db: DB, user: SessionOrTokenUser) -> dict:
    """What your next reminder mail would say, whatever the hour and whatever
    earlier mails said already."""
    return reminders.preview(db, user, utcnow())


@router.put("/projects/{ref}/reminders")
def set_muted(ref: str, body: RemindersIn, db: DB, user: SessionOrTokenUser) -> dict:
    """Mute or unmute your own reminder mails for one paper. Any member may,
    it only concerns them. Returns the summary."""
    project, membership = resolve_project(db, user, ref)
    membership.reminders_muted = body.muted
    db.commit()
    return services.summaries(db, user, [(project, membership)], utcnow())[0]
