"""Project operations the routers share (DESIGN §4 to §7).

Creating a version is reached from two routes (POST /versions and answering a
question), moving a date from two (PATCH and the "moved" answer), and every
write returns a project that was built the same way. Keeping these here means
the rules exist once.

A snapshot stores the live document's updates through its own database
session, in rows that point at the project. Holding a lock on the project row
that blocks foreign keys (FOR UPDATE) while calling live.snapshot() would wait
on ourselves. So a new version takes its snapshot first and then locks the
project row with FOR NO KEY UPDATE, which those inserts do not wait for.
"""

import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DBSession

from . import compiler, lifecycle, live, naming, serialize, tree, when
from .models import (
    Membership,
    Project,
    ProjectDate,
    Review,
    Thread,
    User,
    Version,
    utcnow,
)
from .project_schemas import AnswerIn, DateIn, DatePatch


def new_id() -> str:
    return str(uuid.uuid4())


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


# --- Fields (§4) -----------------------------------------------------------------

# Single-line strings: stripped, and "" when cleared.
_LINE_FIELDS = (
    "title",
    "paper",
    "conference",
    "conference_name",
    "track",
    "cfp_url",
    "conference_url",
    "submission_url",
    "location",
    "publisher",
    "page_limit",
    "template",
    "review_model",
    "doi",
    "publication_url",
)


def clean_labels(labels: list[str] | None) -> list[str]:
    """Stripped, without empties, and each label once (ignoring case)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in labels or []:
        label = str(raw).strip()[:100]
        if label and label.lower() not in seen:
            seen.add(label.lower())
            out.append(label)
    return out


def set_outcome(project: Project, outcome: str | None) -> None:
    """``outcome_at`` says when the outcome was set, and is null without one."""
    if outcome == project.outcome:
        return
    project.outcome = outcome
    project.outcome_at = utcnow() if outcome else None


def apply_fields(project: Project, data: dict) -> None:
    """Set the §4 fields in ``data`` (a pydantic dump with only what was sent)."""
    for key, value in data.items():
        if key == "labels":
            project.labels = clean_labels(value)
        elif key == "outcome":
            set_outcome(project, value)
        elif key == "archived":
            if value is not None:
                project.archived = value
        elif key == "year":
            project.year = value
        elif key == "notes":
            project.notes = value or ""
        elif key == "main_file":
            project.main_file = (value or "").strip() or "main.tex"
        elif key == "compiler":
            project.compiler = value or "pdflatex"
        elif key == "planned":
            if value is not None:
                project.planned = value
        elif key == "writing_days":
            # Null goes back to the owner's usual writing time.
            project.writing_days = value
        elif key in _LINE_FIELDS:
            setattr(project, key, (value or "").strip())


def check_name(name: str | None) -> str:
    name = (name or "").strip()
    if not naming.valid_name(name):
        raise unprocessable(
            "A project name starts with a letter or digit and uses only letters, digits, '.', '_' and '-' "
            "(at most 120 characters)"
        )
    return name


def pattern_name(user: User, fields: dict) -> str:
    """The name the user's pattern gives, or 422 when it comes out empty."""
    name = naming.render(user.naming_pattern, fields)
    if not name:
        raise unprocessable(
            "Your naming pattern gives an empty name for this project. "
            "Fill in conference, year and title, or give a name."
        )
    return name


# --- Dates (§5) ------------------------------------------------------------------


def _tz(value: str | None, default: str) -> str:
    try:
        return when.canonical_tz(value or default)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc


def _due(due_at: str | None, day: str | None, tz: str) -> tuple[datetime, bool]:
    try:
        return when.parse_due(due_at, day, tz)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc


def _date_label(kind: str, label: str | None) -> str:
    label = (label or "").strip()
    if label:
        return label
    if kind == "other":
        raise unprocessable("A date of kind other needs a label")
    return lifecycle.DATE_LABELS[kind]


def new_date(project_id: str, payload: DateIn, position: float) -> ProjectDate:
    tz = _tz(payload.tz, when.DEFAULT_TZ)
    due, date_only = _due(payload.due_at, payload.date, tz)
    return ProjectDate(
        id=new_id(),
        project_id=project_id,
        kind=payload.kind,
        label=_date_label(payload.kind, payload.label),
        due_at=due,
        tz=tz,
        date_only=date_only,
        changes=[],
        position=position,
    )


def _clear_answer(d: ProjectDate) -> None:
    d.answer = None
    d.answered_at = None
    d.answered_by = None


def move_date(d: ProjectDate, due: datetime, tz: str, date_only: bool, user: User) -> None:
    """Set the date's instant. A real move is recorded in ``changes`` and clears
    the answer, because the question is a new one now."""
    if due != d.due_at:
        change = {"at": when.iso(utcnow()), "by": user.email, "from": when.iso(d.due_at), "to": when.iso(due)}
        d.changes = [*(d.changes or []), change]
        _clear_answer(d)
    d.due_at = due
    d.tz = tz
    d.date_only = date_only


def update_date(d: ProjectDate, payload: DatePatch, user: User) -> None:
    data = payload.model_dump(exclude_unset=True)
    kind = data.get("kind") or d.kind
    if "label" in data:
        d.label = _date_label(kind, data["label"])
    elif kind != d.kind and d.label == lifecycle.DATE_LABELS[d.kind]:
        d.label = _date_label(kind, None)
    elif kind == "other" and not d.label:
        raise unprocessable("A date of kind other needs a label")
    if kind != d.kind:
        # The old answer belonged to the old question.
        d.kind = kind
        _clear_answer(d)

    tz = _tz(data["tz"], d.tz) if data.get("tz") else d.tz
    if data.get("due_at") or data.get("date"):
        due, date_only = _due(data.get("due_at"), data.get("date"), tz)
        move_date(d, due, tz, date_only, user)
    elif tz != d.tz:
        move_date(d, when.move_local(d.due_at, d.tz, tz, d.date_only), tz, d.date_only, user)


def find_date(db: DBSession, project: Project, date_id: str) -> ProjectDate:
    d = db.get(ProjectDate, date_id) if _is_uuid(date_id) else None
    if d is None or d.project_id != project.id:
        raise HTTPException(status_code=404, detail="Date not found")
    return d


def next_date_position(db: DBSession, project_id: str) -> float:
    positions = [p for (p,) in db.query(ProjectDate.position).filter(ProjectDate.project_id == project_id).all()]
    return max(positions, default=0.0) + 1.0


# --- Versions (§7) ---------------------------------------------------------------

VERSION_LABELS = {
    "submission": "Submission",
    "rebuttal": "Rebuttal",
    "revision": "Revision {n}",
    "camera_ready": "Camera-ready",
    "arxiv": "arXiv",
    "snapshot": "Snapshot {n}",
    "other": "",
}
VERSION_TAGS = {
    "submission": "submission",
    "rebuttal": "rebuttal",
    "revision": "revision-{n}",
    "camera_ready": "camera-ready",
    "arxiv": "arxiv",
    "snapshot": "snapshot-{n}",
}
_HEX = re.compile(r"^[0-9a-f]{7,64}$")


def freeze(project: Project, at: str | None, date: ProjectDate | None) -> str:
    """The commit a new version points to (§7, ``at``)."""
    pid = project.id
    at = (at or "now").strip()
    if at == "now":
        return live.snapshot(pid)
    if at == "deadline":
        if date is None:
            raise unprocessable("at=deadline needs a date_id. Which date's deadline is meant?")
        if date.due_at >= utcnow():
            # Handed in early. What is there now is what went out.
            return live.snapshot(pid)
        return live.commit_at(pid, date.due_at) or live.snapshot(pid)
    if _HEX.match(at):
        commit = live.resolve_commit(pid, at)
        if commit:
            return commit
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        moment = None
    if moment is not None:
        if moment.tzinfo is not None:
            moment = moment.astimezone(UTC).replace(tzinfo=None)
        commit = live.commit_at(pid, moment)
        if commit is None:
            raise unprocessable(f"There is no snapshot at or before {at}")
        return commit
    commit = live.resolve_commit(pid, at)
    if commit is None:
        raise unprocessable(f"at is now, deadline, an ISO time or a commit. {at} is none of these.")
    return commit


def create_version(
    db: DBSession,
    project: Project,
    user: User,
    kind: str,
    *,
    label: str | None = None,
    note: str | None = None,
    date: ProjectDate | None = None,
    at: str | None = None,
) -> Version:
    """Freeze the files as a new version and tag the commit. The caller commits."""
    label = (label or "").strip()
    if kind == "other" and not label:
        raise unprocessable("A version of kind other needs a label")
    commit = freeze(project, at, date)

    # Numbers and tags are per project. The row lock serialises two versions
    # made at the same moment.
    db.query(Project.id).filter(Project.id == project.id).with_for_update(key_share=True).one()
    existing = db.query(Version).filter(Version.project_id == project.id).all()
    number = max((v.number for v in existing), default=0) + 1
    n = sum(1 for v in existing if v.kind == kind) + 1
    if not label:
        label = VERSION_LABELS[kind].format(n=n)
    if kind == "other":
        base = naming.trim(naming.slug(label).lower(), 100) or "other"
    else:
        base = VERSION_TAGS[kind].format(n=n)
    taken = {v.tag.lower() for v in existing}
    tag, i = base, 1
    # A ref git already knows (a stale tag, the branch "main") is taken too.
    while tag.lower() in taken or live.resolve_commit(project.id, tag) is not None:
        i += 1
        tag = f"{base}-{i}"

    version = Version(
        id=new_id(),
        project_id=project.id,
        number=number,
        kind=kind,
        label=label,
        tag=tag,
        commit=commit,
        date_id=date.id if date else None,
        note=note or "",
        created_by=user.id,
    )
    db.add(version)
    db.flush()
    live.create_tag(project.id, tag, commit)
    project.updated_at = utcnow()
    # §14: a PDF of the version, compiled in the background once committed.
    compiler.compile_version_later(db, project, version)
    return version


def find_version(db: DBSession, project: Project, ref: str) -> Version:
    """A version by id or by tag, or 404."""
    version = db.get(Version, ref) if _is_uuid(ref) else None
    if version is None or version.project_id != project.id:
        version = db.query(Version).filter(Version.project_id == project.id, Version.tag == ref).one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


# --- Answers (§6) ----------------------------------------------------------------


def answer_date(db: DBSession, project: Project, d: ProjectDate, body: AnswerIn, user: User) -> None:
    """Store the answer and do what it means. The caller commits."""
    allowed = lifecycle.answers_for(d.kind)
    if not allowed:
        raise unprocessable(f"A date of kind {d.kind} asks no question")
    answer = body.answer.strip().lower()
    if answer not in allowed:
        raise unprocessable(f"This question takes one of: {', '.join(allowed)}")

    if answer == "moved":
        tz = _tz(body.tz, d.tz)
        if not (body.due_at or body.date):
            raise unprocessable("Moved to when? Give due_at or date.")
        due, date_only = _due(body.due_at, body.date, tz)
        move_date(d, due, tz, date_only, user)
        project.updated_at = utcnow()
        return

    version_kind = lifecycle.VERSION_FOR_YES.get(d.kind) if answer == "yes" else None
    if version_kind:
        made = (
            db.query(Version.id)
            .filter(Version.project_id == project.id, Version.date_id == d.id, Version.kind == version_kind)
            .first()
        )
        if made is None:
            options = body.version
            create_version(
                db,
                project,
                user,
                version_kind,
                note=options.note if options else None,
                date=d,
                at=(options.at if options and options.at else "deadline"),
            )

    if d.kind == "notification":
        set_outcome(project, answer)
    elif d.kind == "revision" and answer == "yes":
        # Under review again. The client asks for the next notification date.
        set_outcome(project, None)
    elif d.kind in ("submission", "abstract"):
        if answer == "no":
            set_outcome(project, "not_submitted")
        elif project.outcome == "not_submitted":
            # A "no" given by mistake, corrected.
            set_outcome(project, None)

    d.answer = answer
    d.answered_at = utcnow()
    d.answered_by = user.id
    project.updated_at = utcnow()


# --- Projects as the API sends them (§11) ------------------------------------------


def dates_by_project(db: DBSession, project_ids: list[str]) -> dict[str, list[ProjectDate]]:
    out: dict[str, list[ProjectDate]] = {pid: [] for pid in project_ids}
    if not project_ids:
        return out
    for d in db.query(ProjectDate).filter(ProjectDate.project_id.in_(project_ids)).all():
        out[d.project_id].append(d)
    for dates in out.values():
        dates.sort(key=lambda d: (d.due_at, d.position))
    return out


def submitted_projects(db: DBSession, project_ids: list[str]) -> set[str]:
    """The projects among ``project_ids`` that have a submission version."""
    if not project_ids:
        return set()
    rows = (
        db.query(Version.project_id)
        .filter(Version.project_id.in_(project_ids), Version.kind == "submission")
        .distinct()
        .all()
    )
    return {pid for (pid,) in rows}


def _folder_path(paths: dict[str, str], membership: Membership) -> str:
    return paths.get(membership.folder_id, "") if membership.folder_id else ""


def open_threads(db: DBSession, user: User, project_ids: list[str]) -> dict[str, int]:
    """Open review threads per project that ``user`` can see: single comments,
    threads of submitted reviews, and the threads of their own pending review
    (DESIGN §13). Counted without anchoring, which costs a diff per thread."""
    if not project_ids:
        return {}
    visible = or_(Thread.review_id.is_(None), Review.state == "submitted", Review.reviewer_id == user.id)
    rows = (
        db.query(Thread.project_id, func.count(Thread.id))
        .outerjoin(Review, Review.id == Thread.review_id)
        .filter(Thread.project_id.in_(project_ids), Thread.state == "open", visible)
        .group_by(Thread.project_id)
        .all()
    )
    return dict(rows)


def summaries(db: DBSession, user: User, rows: list[tuple[Project, Membership]], now: datetime) -> list[dict]:
    ids = [p.id for p, _ in rows]
    dates = dates_by_project(db, ids)
    submitted = submitted_projects(db, ids)
    threads = open_threads(db, user, ids)
    paths = tree.paths(tree.load(db, user.id))
    return [
        serialize.summary(
            p, m, _folder_path(paths, m), dates[p.id], p.id in submitted, now,
            writing_days=user.writing_days, open_threads=threads.get(p.id, 0),
        )
        for p, m in rows
    ]


def detail(db: DBSession, user: User, project: Project, membership: Membership) -> dict:
    """GET /api/projects/{ref}: the summary, every field, and what hangs off it."""
    now = utcnow()
    dates = dates_by_project(db, [project.id])[project.id]
    versions = db.query(Version).filter(Version.project_id == project.id).order_by(Version.number).all()
    members = (
        db.query(Membership, User)
        .join(User, User.id == Membership.user_id)
        .filter(Membership.project_id == project.id)
        .all()
    )
    users = {u.id: u for _, u in members}
    missing = {v.created_by for v in versions if v.created_by and v.created_by not in users}
    missing |= {project.owner_id} - users.keys()
    if missing:
        users.update({u.id: u for u in db.query(User).filter(User.id.in_(missing)).all()})
    has_submission = any(v.kind == "submission" for v in versions)

    paths = tree.paths(tree.load(db, user.id))
    out = serialize.summary(
        project, membership, _folder_path(paths, membership), dates, has_submission, now,
        writing_days=user.writing_days, open_threads=open_threads(db, user, [project.id]).get(project.id, 0),
    )
    out.update(serialize.detail_fields(project))
    out["owner"] = serialize.person(users.get(project.owner_id))
    out["dates"] = [serialize.date_dict(d, now) for d in dates]
    out["questions"] = lifecycle.questions(project, dates, has_submission, now)
    out["versions"] = [serialize.version_dict(v, users.get(v.created_by or "")) for v in versions]
    members.sort(key=lambda mu: (mu[0].role != "owner", mu[0].added_at))
    out["members"] = [serialize.member_dict(m, u) for m, u in members]
    source = db.get(Project, project.forked_from_id) if project.forked_from_id else None
    out["forked_from"] = {"id": source.id, "name": source.name} if source else None
    out["file_count"] = len(live.list_files(project.id))
    return out
