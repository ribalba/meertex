"""The timeline: every paper with its dates and writing window, for the Gantt
chart (docs/DESIGN.md §3, The timeline), and its public, read-only link
(§3, Sharing the timeline)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.orm import Session as DBSession

from .. import serialize, services, tree
from ..database import DB
from ..models import Membership, Project, TimelineShare, User, utcnow
from ..project_schemas import TimelineShareIn
from ..security import SessionOrTokenUser, new_token
from ..when import iso

router = APIRouter(prefix="/api", tags=["timeline"])


def _rows(db: DBSession, user: User, archived: str, now: datetime, folder_ids: set[str] | None = None) -> list[dict]:
    """The user's projects as summaries with all their dates, the ones to start
    writing first on top, then by their next date. ``folder_ids`` keeps only
    the projects the user put into one of those folders."""
    query = (
        db.query(Project, Membership)
        .join(Membership, Membership.project_id == Project.id)
        .filter(Membership.user_id == user.id)
    )
    if archived != "all":
        query = query.filter(Project.archived.is_(archived == "true"))
    if folder_ids is not None:
        query = query.filter(Membership.folder_id.in_(folder_ids))
    rows = query.all()
    out = services.summaries(db, user, rows, now)
    dates = services.dates_by_project(db, [p.id for p, _ in rows])
    for s in out:
        s["dates"] = [serialize.date_dict(d, now) for d in dates[s["id"]]]

    def order(s: dict) -> tuple:
        start = (s.get("writing") or {}).get("start")
        nd = (s.get("next_date") or {}).get("due_at")
        far = "9999"
        return (start or far, nd or far, s["name"].lower())

    out.sort(key=order)
    return out


@router.get("/timeline")
def timeline(db: DB, user: SessionOrTokenUser, archived: str = "false") -> dict:
    """Your projects as summaries with all their dates, the ones you have to
    start writing first on top, then by their next date."""
    archived = archived.strip().lower()
    if archived not in ("false", "true", "all"):
        raise services.unprocessable("archived is false, true or all")
    now = utcnow()
    return {"writing_days": user.writing_days, "today": iso(now), "projects": _rows(db, user, archived, now)}


# --- Sharing the timeline ------------------------------------------------------------


def _share_out(db: DBSession, share: TimelineShare) -> dict:
    path = tree.paths(tree.load(db, share.user_id)).get(share.folder_id) if share.folder_id else None
    return serialize.timeline_share_dict(share, path)


def _folder_id(db: DBSession, user: User, ref: str | None) -> str | None:
    """The folder a link shows, by id or path. None for every paper, 404 for a
    folder that is not yours."""
    folder = tree.resolve(db, user.id, ref)
    return folder.id if folder else None


@router.get("/timeline/share")
def get_share(db: DB, user: SessionOrTokenUser) -> dict | None:
    """Your public link to the timeline, or null when you share none."""
    share = db.get(TimelineShare, user.id)
    return _share_out(db, share) if share else None


@router.post("/timeline/share", status_code=201)
def new_share(db: DB, user: SessionOrTokenUser, body: TimelineShareIn | None = None) -> dict:
    """A new public link. The one you had before stops working."""
    folder_id = _folder_id(db, user, body.folder if body else None)
    share = db.get(TimelineShare, user.id)
    if share is None:
        share = TimelineShare(user_id=user.id, token=new_token())
        db.add(share)
    else:
        share.token = new_token()
        share.created_at = utcnow()
    share.folder_id = folder_id
    db.commit()
    return _share_out(db, share)


@router.patch("/timeline/share")
def change_share(body: TimelineShareIn, db: DB, user: SessionOrTokenUser) -> dict:
    """Which papers the link shows. The link itself stays the same."""
    share = db.get(TimelineShare, user.id)
    if share is None:
        raise HTTPException(status_code=404, detail="You share no timeline. Make a link first.")
    if "folder" in body.model_fields_set:
        share.folder_id = _folder_id(db, user, body.folder)
        db.commit()
    return _share_out(db, share)


@router.delete("/timeline/share")
def stop_share(db: DB, user: SessionOrTokenUser) -> dict:
    """Stop sharing. The link stops working at once."""
    db.query(TimelineShare).filter(TimelineShare.user_id == user.id).delete()
    db.commit()
    return {"ok": True}


@router.get("/shared/timeline/{token}")
def shared_timeline(token: str, db: DB, response: Response) -> dict:
    """The timeline behind a public link, for anyone who has the link and
    without signing in. The papers come with their venue, dates and writing
    windows and nothing else (serialize.shared_project)."""
    share = db.query(TimelineShare).filter(TimelineShare.token == token).one_or_none()
    owner = db.get(User, share.user_id) if share else None
    if share is None or owner is None:
        raise HTTPException(status_code=404, detail="This link does not work. It was replaced, or the timeline is no longer shared.")
    folder_ids = tree.subtree(tree.load(db, owner.id), share.folder_id) if share.folder_id else None
    now = utcnow()
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return {
        "owner": owner.name or None,
        "writing_days": owner.writing_days,
        "today": iso(now),
        "projects": [serialize.shared_project(s) for s in _rows(db, owner, "false", now, folder_ids)],
    }
