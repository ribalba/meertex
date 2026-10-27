"""Each user's own folder tree (DESIGN §3).

Folders are personal, so every query here is scoped to one user. A user has a
few dozen folders at most, which is why the tree is loaded whole and walked in
Python instead of with recursive SQL.
"""

import uuid
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from .models import Folder, Membership


def new_id() -> str:
    return str(uuid.uuid4())


def load(db: DBSession, user_id: str) -> list[Folder]:
    return db.query(Folder).filter(Folder.user_id == user_id).all()


def _children(folders: list[Folder]) -> dict[str | None, list[Folder]]:
    kids: dict[str | None, list[Folder]] = defaultdict(list)
    for f in folders:
        kids[f.parent_id].append(f)
    for siblings in kids.values():
        siblings.sort(key=lambda f: (f.position, f.name.lower()))
    return kids


def ordered(folders: list[Folder]) -> list[Folder]:
    """Depth first, siblings by position. A folder whose parent is missing
    (it should not happen) is left out rather than looping."""
    kids = _children(folders)
    out: list[Folder] = []
    stack = list(reversed(kids.get(None, [])))
    seen: set[str] = set()
    while stack:
        f = stack.pop()
        if f.id in seen:
            continue
        seen.add(f.id)
        out.append(f)
        stack.extend(reversed(kids.get(f.id, [])))
    return out


def paths(folders: list[Folder]) -> dict[str, str]:
    """Folder id to its path, the names joined by "/"."""
    by_id = {f.id: f for f in folders}
    out: dict[str, str] = {}

    def path_of(f: Folder, depth: int = 0) -> str:
        if f.id in out:
            return out[f.id]
        parent = by_id.get(f.parent_id) if f.parent_id else None
        if parent is None or depth > 100:
            out[f.id] = f.name
        else:
            out[f.id] = path_of(parent, depth + 1) + "/" + f.name
        return out[f.id]

    for f in folders:
        path_of(f)
    return out


def subtree(folders: list[Folder], folder_id: str) -> set[str]:
    """``folder_id`` and every folder below it."""
    kids = _children(folders)
    found = {folder_id}
    stack = [folder_id]
    while stack:
        for child in kids.get(stack.pop(), []):
            if child.id not in found:
                found.add(child.id)
                stack.append(child.id)
    return found


def segments(path: str) -> list[str]:
    return [s.strip() for s in path.split("/") if s.strip()]


def clean_name(name: str | None) -> str:
    """A folder name, or 422. "/" would break paths."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="A folder needs a name")
    if "/" in name:
        raise HTTPException(status_code=422, detail="A folder name cannot contain /")
    if len(name) > 200:
        raise HTTPException(status_code=422, detail="A folder name has at most 200 characters")
    return name


def find_child(folders: list[Folder], parent_id: str | None, name: str, exclude_id: str | None = None) -> Folder | None:
    """The sibling under ``parent_id`` called ``name``, ignoring case."""
    wanted = name.lower()
    for f in folders:
        if f.parent_id == parent_id and f.name.lower() == wanted and f.id != exclude_id:
            return f
    return None


def check_free(folders: list[Folder], parent_id: str | None, name: str, exclude_id: str | None = None) -> None:
    if find_child(folders, parent_id, name, exclude_id) is not None:
        raise HTTPException(status_code=409, detail=f"There is already a folder called {name} here")


def next_position(db: DBSession, user_id: str, parent_id: str | None) -> float:
    """After the last folder among the siblings."""
    where = Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id
    top = db.query(func.max(Folder.position)).filter(Folder.user_id == user_id, where).scalar()
    return (top or 0.0) + 1.0


def next_project_position(db: DBSession, user_id: str, folder_id: str | None) -> float:
    """After the last project the user put into ``folder_id``."""
    where = Membership.folder_id.is_(None) if folder_id is None else Membership.folder_id == folder_id
    top = db.query(func.max(Membership.position)).filter(Membership.user_id == user_id, where).scalar()
    return (top or 0.0) + 1.0


def get_own(db: DBSession, user_id: str, folder_id: str) -> Folder:
    """The user's folder ``folder_id``, or 404. Someone else's folder is 404 too."""
    folder = db.get(Folder, folder_id)
    if folder is None or folder.user_id != user_id:
        raise HTTPException(status_code=404, detail="Folder not found")
    return folder


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def resolve(db: DBSession, user_id: str, ref: str | None, create: bool = False) -> Folder | None:
    """A folder given by id or by path. None for the top level (empty ref).
    A missing path is created like ``mkdir -p`` when ``create``, else 404."""
    ref = (ref or "").strip()
    if not ref or ref == "/":
        return None
    if _is_uuid(ref):
        return get_own(db, user_id, ref)
    folders = load(db, user_id)
    parent: Folder | None = None
    for name in segments(ref):
        found = find_child(folders, parent.id if parent else None, name)
        if found is None:
            if not create:
                raise HTTPException(status_code=404, detail=f"There is no folder {ref}")
            found = Folder(
                id=new_id(),
                user_id=user_id,
                parent_id=parent.id if parent else None,
                name=clean_name(name),
                position=next_position(db, user_id, parent.id if parent else None),
            )
            db.add(found)
            db.flush()
            folders.append(found)
        parent = found
    return parent
