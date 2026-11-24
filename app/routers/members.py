"""Who is on a project, inviting coauthors, and sharing a project with a link
(DESIGN §2, §11).

An invite works for an email that has no account yet: the account is made on
the spot, so the membership exists before the person ever signs in, and the
mailed link signs them in and lands on the project.

A share link works the other way round. Whoever opens it signs in first, with
any email, and then joins the project in the link's role.
"""

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from .. import live, serialize
from ..access import resolve_project
from ..config import get_settings
from ..database import DB
from ..emailer import send_invite
from ..models import Membership, Project, ProjectShare, User, utcnow
from ..project_schemas import MemberIn, MemberPatch, ProjectShareIn
from ..security import (
    SessionOrTokenUser,
    create_login_token,
    get_or_create_user,
    new_token,
)
from ..tree import next_project_position

router = APIRouter(prefix="/api", tags=["members"])


def _member(db: DB, project_id: str, user_id: str) -> tuple[Membership, User]:
    row = (
        db.query(Membership, User)
        .join(User, User.id == Membership.user_id)
        .filter(Membership.project_id == project_id, Membership.user_id == user_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Not a member of this project")
    return row


def _add(db: DBSession, project_id: str, user_id: str, role: str, added_by: str | None) -> Membership:
    """A new membership, at the end of the person's top level."""
    added = Membership(
        project_id=project_id,
        user_id=user_id,
        role=role,
        folder_id=None,
        position=next_project_position(db, user_id, None),
        added_at=utcnow(),
        added_by=added_by,
    )
    db.add(added)
    return added


@router.get("/people")
def list_people(db: DB, user: SessionOrTokenUser) -> list[dict]:
    """Everyone who shares a paper with the user, for the email suggestions
    of "Add someone". Those on the most papers with them come first."""
    mine = db.query(Membership.project_id).filter(Membership.user_id == user.id)
    shared = func.count(Membership.project_id)
    rows = (
        db.query(User, shared)
        .join(Membership, Membership.user_id == User.id)
        .filter(Membership.project_id.in_(mine), User.id != user.id)
        .group_by(User.id)
        .order_by(shared.desc(), User.email)
        .all()
    )
    return [{"user_id": u.id, "email": u.email, "name": u.name, "papers": n} for u, n in rows]


@router.get("/projects/{ref}/members")
def list_members(ref: str, db: DB, user: SessionOrTokenUser) -> list[dict]:
    project, _ = resolve_project(db, user, ref)
    rows = (
        db.query(Membership, User)
        .join(User, User.id == Membership.user_id)
        .filter(Membership.project_id == project.id)
        .all()
    )
    rows.sort(key=lambda r: (r[0].role != "owner", r[0].added_at))
    return [serialize.member_dict(m, u) for m, u in rows]


@router.post("/projects/{ref}/members", status_code=201)
def add_member(ref: str, body: MemberIn, db: DB, user: SessionOrTokenUser) -> dict:
    project, membership = resolve_project(db, user, ref, "editor")
    if body.role == "editor" and membership.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can add editors")
    invited = get_or_create_user(db, body.email)
    if db.get(Membership, (project.id, invited.id)) is not None:
        raise HTTPException(status_code=409, detail=f"{invited.email} is already a member")
    added = _add(db, project.id, invited.id, body.role, user.id)
    db.commit()

    token = create_login_token(db, invited.email, redirect_to=f"/p/{project.id}", expires=False)
    link = f"{get_settings().base_url.rstrip('/')}/api/auth/callback?token={token}"
    send_invite(invited.email, user.name or user.email, project.title or project.name, body.role, link)
    return serialize.member_dict(added, invited)


@router.patch("/projects/{ref}/members/{user_id}")
def change_role(ref: str, user_id: str, body: MemberPatch, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref, "owner")
    target, person = _member(db, project.id, user_id)
    if target.role == "owner":
        raise HTTPException(status_code=422, detail="The owner's role cannot be changed")
    changed = target.role != body.role
    target.role = body.role
    db.commit()
    if changed:
        live.member_role_changed(project.id, person.email)
    return serialize.member_dict(target, person)


@router.delete("/projects/{ref}/members/{user_id}")
def remove_member(ref: str, user_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    """The owner removes anyone but themselves. A member may leave."""
    project, membership = resolve_project(db, user, ref)
    if user_id != user.id and membership.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can remove other members")
    target, person = _member(db, project.id, user_id)
    if target.role == "owner":
        raise HTTPException(status_code=422, detail="The owner cannot be removed")
    db.delete(target)
    db.commit()
    live.member_left(project.id, person.email)
    return {"ok": True}


# --- Sharing with a link --------------------------------------------------------------


def _share_out(db: DBSession, share: ProjectShare) -> dict:
    return serialize.project_share_dict(share, db.get(User, share.created_by) if share.created_by else None)


def _may_grant(membership: Membership, role: str) -> None:
    """The same rule as for inviting. Editors hand out reviewer and viewer
    links, and only the owner a link that makes editors."""
    if role == "editor" and membership.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can share a link that makes editors")


def _share_of(db: DBSession, project_id: str) -> ProjectShare | None:
    return db.get(ProjectShare, project_id)


@router.get("/projects/{ref}/share")
def get_share(ref: str, db: DB, user: SessionOrTokenUser) -> dict | None:
    """The project's join link, or null when it has none."""
    project, _ = resolve_project(db, user, ref, "editor")
    share = _share_of(db, project.id)
    return _share_out(db, share) if share else None


@router.post("/projects/{ref}/share", status_code=201)
def new_share(ref: str, db: DB, user: SessionOrTokenUser, body: ProjectShareIn | None = None) -> dict:
    """A new join link. The one the project had before stops working."""
    project, membership = resolve_project(db, user, ref, "editor")
    role = body.role if body else "viewer"
    _may_grant(membership, role)
    share = _share_of(db, project.id)
    if share is None:
        share = ProjectShare(project_id=project.id, token=new_token())
        db.add(share)
    else:
        _may_grant(membership, share.role)
        share.token = new_token()
        share.created_at = utcnow()
    share.role = role
    share.created_by = user.id
    db.commit()
    return _share_out(db, share)


@router.patch("/projects/{ref}/share")
def change_share(ref: str, body: ProjectShareIn, db: DB, user: SessionOrTokenUser) -> dict:
    """The role the link gives. The link itself stays the same, and people
    who joined with it keep the role they have."""
    project, membership = resolve_project(db, user, ref, "editor")
    share = _share_of(db, project.id)
    if share is None:
        raise HTTPException(status_code=404, detail="This paper has no link. Make one first.")
    _may_grant(membership, share.role)
    _may_grant(membership, body.role)
    share.role = body.role
    db.commit()
    return _share_out(db, share)


@router.delete("/projects/{ref}/share")
def stop_share(ref: str, db: DB, user: SessionOrTokenUser) -> dict:
    """Stop sharing. The link stops working at once. Who joined with it stays."""
    project, membership = resolve_project(db, user, ref, "editor")
    share = _share_of(db, project.id)
    if share is not None:
        _may_grant(membership, share.role)
        db.delete(share)
        db.commit()
    return {"ok": True}


def _shared(db: DBSession, token: str) -> tuple[ProjectShare, Project]:
    share = db.query(ProjectShare).filter(ProjectShare.token == token).one_or_none()
    project = db.get(Project, share.project_id) if share else None
    if share is None or project is None:
        raise HTTPException(
            status_code=404,
            detail="This link does not work. It was replaced, or the paper is no longer shared. Ask for a new link.",
        )
    return share, project


# The path parameter is not called ``token``: the auth dependency already reads
# a ``?token=`` (the API token) of that name.
@router.get("/join/{link}")
def join_preview(link: str, db: DB, user: SessionOrTokenUser, response: Response) -> dict:
    """What the link leads to, before joining. ``member_role`` is the role
    the user already has on the paper, or null."""
    share, project = _shared(db, link)
    by = db.get(User, share.created_by) if share.created_by else None
    mine = db.get(Membership, (project.id, user.id))
    response.headers["Cache-Control"] = "no-store"
    return {
        "project": {"id": project.id, "name": project.name, "title": project.title},
        "role": share.role,
        "shared_by": {"email": by.email, "name": by.name} if by else None,
        "member_role": mine.role if mine else None,
    }


@router.post("/join/{link}")
def join(link: str, db: DB, user: SessionOrTokenUser) -> dict:
    """Join the paper in the link's role. A member keeps the role they have,
    so a link never lowers or raises anyone."""
    share, project = _shared(db, link)
    mine = db.get(Membership, (project.id, user.id))
    if mine is not None:
        return {"project_id": project.id, "role": mine.role, "joined": False}
    added = _add(db, project.id, user.id, share.role, share.created_by)
    try:
        db.commit()
    except IntegrityError:
        # A second click that came in at the same time already joined.
        db.rollback()
        mine = db.get(Membership, (project.id, user.id))
        return {"project_id": project.id, "role": mine.role if mine else share.role, "joined": False}
    return {"project_id": project.id, "role": added.role, "joined": True}
