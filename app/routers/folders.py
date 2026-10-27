"""Your folder tree (DESIGN §3, §11).

Folders are personal: nobody else sees them, so a folder of another user is
404 like one that does not exist. Deleting only works on an empty folder. The
client moves things out first, so nothing disappears by accident.
"""

from fastapi import APIRouter, HTTPException

from .. import serialize, tree
from ..database import DB
from ..models import Folder, Membership
from ..project_schemas import FolderCreate, FolderPatch
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api", tags=["folders"])


def _out(db: DB, user_id: str, folder: Folder) -> dict:
    paths = tree.paths(tree.load(db, user_id))
    return serialize.folder_dict(folder, paths.get(folder.id, folder.name))


@router.get("/folders")
def list_folders(db: DB, user: SessionOrTokenUser) -> list[dict]:
    """Depth first, siblings by position."""
    folders = tree.load(db, user.id)
    paths = tree.paths(folders)
    return [serialize.folder_dict(f, paths[f.id]) for f in tree.ordered(folders)]


@router.post("/folders", status_code=201)
def create_folder(body: FolderCreate, db: DB, user: SessionOrTokenUser) -> dict:
    name = tree.clean_name(body.name)
    if body.parent_id:
        parent = tree.get_own(db, user.id, body.parent_id)
    else:
        parent = tree.resolve(db, user.id, body.parent, create=True)
    parent_id = parent.id if parent else None
    tree.check_free(tree.load(db, user.id), parent_id, name)
    folder = Folder(
        id=tree.new_id(),
        user_id=user.id,
        parent_id=parent_id,
        name=name,
        position=tree.next_position(db, user.id, parent_id),
    )
    db.add(folder)
    db.commit()
    return _out(db, user.id, folder)


@router.patch("/folders/{folder_id}")
def update_folder(folder_id: str, body: FolderPatch, db: DB, user: SessionOrTokenUser) -> dict:
    folder = tree.get_own(db, user.id, folder_id)
    data = body.model_dump(exclude_unset=True)
    folders = tree.load(db, user.id)
    parent_id = folder.parent_id
    if "parent_id" in data:
        parent_id = data["parent_id"] or None
        if parent_id is not None:
            tree.get_own(db, user.id, parent_id)
            if parent_id in tree.subtree(folders, folder.id):
                raise HTTPException(status_code=422, detail="A folder cannot move into itself or one of its subfolders")
    name = tree.clean_name(data["name"]) if "name" in data else folder.name
    if parent_id != folder.parent_id or name.lower() != folder.name.lower():
        tree.check_free(folders, parent_id, name, exclude_id=folder.id)
    if parent_id != folder.parent_id and data.get("position") is None:
        folder.position = tree.next_position(db, user.id, parent_id)
    folder.parent_id = parent_id
    folder.name = name
    if data.get("position") is not None:
        folder.position = data["position"]
    db.commit()
    return _out(db, user.id, folder)


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    folder = tree.get_own(db, user.id, folder_id)
    has_folders = db.query(Folder.id).filter(Folder.parent_id == folder.id).first() is not None
    has_projects = db.query(Membership.project_id).filter(Membership.folder_id == folder.id).first() is not None
    if has_folders or has_projects:
        raise HTTPException(status_code=409, detail="The folder is not empty. Move its folders and projects out first.")
    db.delete(folder)
    db.commit()
    return {"ok": True}
