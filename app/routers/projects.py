"""Projects: the list, creating, reading, editing, forking and deleting
(DESIGN §4, §11).

A project's rows are written and committed before its files are made, because
the live document stores its updates in rows that point at the project. When
making the files fails, the rows are removed again, so a half-made project
never shows up in anybody's list.
"""

import logging

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from .. import live, naming, services, tree
from ..access import resolve_project
from ..database import DB
from ..models import OUTCOMES, Membership, Project, User, utcnow
from ..project_schemas import PlacementIn, ProjectCreate, ProjectPatch, StartIn
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api", tags=["projects"])
logger = logging.getLogger("uvicorn.error")

# What a fork takes over from its source unless the body says otherwise. The
# venue's fields, the dates, the versions and the outcome stay behind.
FORK_KEEPS = ("title", "paper", "main_file", "compiler", "labels")
_CREATE_ONLY = {"name", "folder", "dates", "dry_run", "starter"}
_ATTEMPTS = 5


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/projects")
def list_projects(
    db: DB,
    user: SessionOrTokenUser,
    outcome: str | None = None,
    label: str | None = None,
    folder: str | None = None,
    q: str | None = None,
    year: int | None = None,
    conference: str | None = None,
    archived: str = "false",
    planned: bool | None = None,
) -> list[dict]:
    query = (
        db.query(Project, Membership)
        .join(Membership, Membership.project_id == Project.id)
        .filter(Membership.user_id == user.id)
    )
    archived = archived.strip().lower()
    if archived not in ("false", "true", "all"):
        raise services.unprocessable("archived is false, true or all")
    if archived != "all":
        query = query.filter(Project.archived.is_(archived == "true"))
    if planned is not None:
        query = query.filter(Project.planned.is_(planned))
    if outcome:
        wanted = [o.strip().lower() for o in outcome.split(",") if o.strip()]
        unknown = [o for o in wanted if o != "none" and o not in OUTCOMES]
        if unknown:
            raise services.unprocessable(f"Unknown outcome {unknown[0]}. Use one of: none, {', '.join(OUTCOMES)}")
        named = [o for o in wanted if o != "none"]
        conditions = [Project.outcome.in_(named)] if named else []
        if "none" in wanted:
            conditions.append(Project.outcome.is_(None))
        query = query.filter(or_(*conditions))
    if year is not None:
        query = query.filter(Project.year == year)
    if conference:
        query = query.filter(func.lower(Project.conference) == conference.strip().lower())
    if q and q.strip():
        pattern = f"%{_escape_like(q.strip())}%"
        columns = (
            Project.name,
            Project.title,
            Project.paper,
            Project.conference,
            Project.conference_name,
            Project.notes,
        )
        query = query.filter(or_(*(c.ilike(pattern, escape="\\") for c in columns)))

    order: dict[str, int] = {}
    if folder is not None and folder.strip():
        start = tree.resolve(db, user.id, folder)
        folders = tree.load(db, user.id)
        if start is None:
            # "/" is the whole tree: the top level first, then every folder.
            inside = [f.id for f in tree.ordered(folders)]
            order = {"": 0} | {fid: i + 1 for i, fid in enumerate(inside)}
        else:
            inside_set = tree.subtree(folders, start.id)
            inside = [f.id for f in tree.ordered(folders) if f.id in inside_set]
            order = {fid: i for i, fid in enumerate(inside)}
            query = query.filter(Membership.folder_id.in_(inside))

    rows = query.all()
    if label and label.strip():
        wanted_label = label.strip().lower()
        rows = [(p, m) for p, m in rows if any(lbl.lower() == wanted_label for lbl in (p.labels or []))]

    now = utcnow()
    if order:
        rows.sort(key=lambda r: (order.get(r[1].folder_id or "", len(order)), r[1].position, r[0].name.lower()))
        return services.summaries(db, user, rows, now)

    # Next date first, soonest on top. Then the most recently updated.
    rows.sort(key=lambda r: r[0].updated_at, reverse=True)
    out = services.summaries(db, user, rows, now)
    with_date = sorted((s for s in out if s["next_date"]), key=lambda s: s["next_date"]["due_at"])
    return with_date + [s for s in out if not s["next_date"]]


def _project_fields(body: ProjectCreate, base: dict | None = None) -> tuple[dict, str]:
    """The §4 fields to set from a create body, on top of ``base``, and the
    starting files."""
    fields = dict(base or {})
    fields.update(body.model_dump(exclude_unset=True, exclude=_CREATE_ONLY))
    return fields, body.starter


def _insert(
    db: DB,
    user: User,
    fields: dict,
    paper: str,
    base_name: str,
    body: ProjectCreate,
    folder_ref: str | None,
    source: Project | None,
) -> Project:
    """The project's rows: the project, the owner's membership in their folder
    (created like mkdir -p), and the dates. Not committed."""
    project = Project(id=services.new_id(), owner_id=user.id, labels=[])
    services.apply_fields(project, fields)
    project.paper = paper or ""
    project.name = naming.unique_name(db, user.id, base_name)
    project.forked_from_id = source.id if source else None
    db.add(project)
    db.flush()
    folder = tree.resolve(db, user.id, folder_ref, create=True)
    folder_id = folder.id if folder else None
    db.add(
        Membership(
            project_id=project.id,
            user_id=user.id,
            role="owner",
            folder_id=folder_id,
            position=tree.next_project_position(db, user.id, folder_id),
            added_by=user.id,
        )
    )
    for i, d in enumerate(body.dates):
        db.add(services.new_date(project.id, d, float(i + 1)))
    db.flush()
    return project


def _new_project(
    db: DB,
    user: User,
    body: ProjectCreate,
    response: Response,
    base: dict | None = None,
    source: Project | None = None,
    default_folder: str | None = None,
) -> dict:
    """POST /api/projects and POST .../fork: name, rows, then files."""
    fields, starter = _project_fields(body, base)
    draft = Project(owner_id=user.id, labels=[])
    services.apply_fields(draft, fields)
    if not draft.paper and draft.title:
        draft.paper = naming.derive_paper(draft.title)
    naming_fields = {
        "year": draft.year,
        "conference": draft.conference,
        "paper": draft.paper,
        "track": draft.track,
        "title": draft.title,
    }
    base_name = services.check_name(body.name) if body.name else services.pattern_name(user, naming_fields)
    # Every date is checked before anything is written.
    for d in body.dates:
        services.new_date("", d, 0.0)

    if body.dry_run:
        response.status_code = 200
        return {"name": naming.unique_name(db, user.id, base_name), "dry_run": True}

    folder_ref = body.folder if "folder" in body.model_fields_set else default_folder
    project = None
    for attempt in range(_ATTEMPTS):
        try:
            project = _insert(db, user, fields, draft.paper, base_name, body, folder_ref, source)
            db.commit()
            break
        except IntegrityError:
            # Someone took the name between the check and the insert.
            db.rollback()
            if attempt == _ATTEMPTS - 1:
                raise HTTPException(status_code=409, detail="Could not find a free name. Try again.") from None

    try:
        if source is not None:
            live.fork_project(source.id, project.id, f"Resubmission of {source.name}")
        else:
            live.init_project(project.id, starter)
    except Exception as exc:
        logger.exception("making the files of project %s failed", project.id)
        db.rollback()
        db.query(Project).filter(Project.id == project.id).delete()
        db.commit()
        live.delete_project(project.id)
        raise HTTPException(status_code=500, detail="The project's files could not be made. Nothing was kept.") from exc

    membership = db.get(Membership, (project.id, user.id))
    return services.detail(db, user, project, membership)


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, response: Response, db: DB, user: SessionOrTokenUser) -> dict:
    return _new_project(db, user, body, response)


@router.get("/projects/{ref}")
def get_project(ref: str, db: DB, user: SessionOrTokenUser) -> dict:
    project, membership = resolve_project(db, user, ref)
    return services.detail(db, user, project, membership)


@router.patch("/projects/{ref}")
def update_project(ref: str, body: ProjectPatch, db: DB, user: SessionOrTokenUser) -> dict:
    project, membership = resolve_project(db, user, ref, "editor")
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        name = services.check_name(data.pop("name"))
        if name != project.name:
            if naming.unique_name(db, project.owner_id, name, exclude_id=project.id) != name:
                raise HTTPException(status_code=409, detail=f"The owner already has a project called {name}")
            project.name = name
    services.apply_fields(project, data)
    project.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="The owner already has a project with that name") from None
    return services.detail(db, user, project, membership)


@router.delete("/projects/{ref}")
def delete_project(ref: str, db: DB, user: SessionOrTokenUser, confirm: str | None = None) -> dict:
    project, _ = resolve_project(db, user, ref, "owner")
    if confirm != project.name:
        raise services.unprocessable(f"To delete this project, pass confirm={project.name}")
    project_id = project.id
    db.query(Project).filter(Project.id == project_id).delete()
    db.commit()
    live.delete_project(project_id)
    return {"ok": True}


@router.post("/projects/{ref}/fork", status_code=201)
def fork_project(ref: str, body: ProjectCreate, response: Response, db: DB, user: SessionOrTokenUser) -> dict:
    source, membership = resolve_project(db, user, ref, "editor")
    base = {field: getattr(source, field) for field in FORK_KEEPS}
    return _new_project(db, user, body, response, base=base, source=source, default_folder=membership.folder_id)


@router.post("/projects/{ref}/start")
def start_project(ref: str, body: StartIn, db: DB, user: SessionOrTokenUser) -> dict:
    """Turn a plan into a paper (DESIGN §4, Planned papers). The name is built
    again from the owner's pattern, now that the paper has a title, unless
    ``keep_name``. Starter files are only written into an empty project."""
    project, membership = resolve_project(db, user, ref, "editor")
    if not project.planned:
        raise services.unprocessable("This is a paper already, not a plan")
    title = body.title.strip()
    if not title:
        raise services.unprocessable("A paper needs a title")
    project.title = title
    project.paper = (body.paper or "").strip() or naming.derive_paper(title)
    project.planned = False
    if not body.keep_name:
        owner = db.get(User, project.owner_id)
        fields = {
            "year": project.year,
            "conference": project.conference,
            "paper": project.paper,
            "track": project.track,
            "title": project.title,
        }
        base = services.pattern_name(owner, fields)
        project.name = naming.unique_name(db, project.owner_id, base, exclude_id=project.id)
    project.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not find a free name. Try again.") from None
    if body.starter == "basic" and not live.list_files(project.id):
        live.add_starter_files(project.id, user.email)
    return services.detail(db, user, project, membership)


@router.put("/projects/{ref}/placement")
def place_project(ref: str, body: PlacementIn, db: DB, user: SessionOrTokenUser) -> dict:
    """Move the project in *your* tree. Any member may, it is their tree."""
    project, membership = resolve_project(db, user, ref)
    folder_id = tree.get_own(db, user.id, body.folder_id).id if body.folder_id else None
    membership.folder_id = folder_id
    if body.position is not None:
        membership.position = body.position
    else:
        membership.position = tree.next_project_position(db, user.id, folder_id)
    db.commit()
    return services.summaries(db, user, [(project, membership)], utcnow())[0]

