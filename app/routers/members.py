"""Who is on a project, and inviting coauthors (DESIGN §2, §11).

An invite works for an email that has no account yet: the account is made on
the spot, so the membership exists before the person ever signs in, and the
mailed link signs them in and lands on the project.
"""

from fastapi import APIRouter, HTTPException
from sqlalchemy import func

from .. import live, serialize
from ..access import resolve_project
from ..config import get_settings
from ..database import DB
from ..emailer import send_invite
from ..models import Membership, User, utcnow
from ..project_schemas import MemberIn, MemberPatch
from ..security import SessionOrTokenUser, create_login_token, get_or_create_user
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
    added = Membership(
        project_id=project.id,
        user_id=invited.id,
        role=body.role,
        folder_id=None,
        position=next_project_position(db, invited.id, None),
        added_at=utcnow(),
        added_by=user.id,
    )
    db.add(added)
    db.commit()

    token = create_login_token(db, invited.email, redirect_to=f"/p/{project.id}")
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
