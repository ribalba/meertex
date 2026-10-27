"""Rows to the JSON object shapes of DESIGN §11.

The web app, the CLI and agents all read these shapes, so each one is built in
exactly one place. Datetimes go out as ISO 8601 UTC with ``Z``: they are naive
UTC inside, and a client must never read them as local time.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import object_session

from . import lifecycle, when
from .config import get_settings
from .models import (
    Compile,
    Folder,
    Membership,
    Project,
    ProjectDate,
    TimelineShare,
    User,
    Version,
    utcnow,
)

iso = when.iso

# §4's fields as the full project shows them, besides the summary's.
DETAIL_FIELDS = (
    "conference_name",
    "cfp_url",
    "conference_url",
    "submission_url",
    "location",
    "publisher",
    "page_limit",
    "template",
    "review_model",
    "notes",
    "main_file",
    "compiler",
    "doi",
    "publication_url",
    "forked_from_id",
    "owner_id",
)


def project_url(name: str) -> str:
    return f"{get_settings().base_url.rstrip('/')}/p/{name}"


def person(user: User | None) -> dict | None:
    if user is None:
        return None
    return {"id": user.id, "email": user.email, "name": user.name, "avatar_url": user.avatar_url}


def folder_dict(folder: Folder, path: str) -> dict:
    return {
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "path": path,
        "position": folder.position,
    }


def _seconds_left(d: ProjectDate, now: datetime) -> int:
    return int((d.due_at - now).total_seconds())


def date_dict(d: ProjectDate, now: datetime) -> dict:
    return {
        "id": d.id,
        "kind": d.kind,
        "label": d.label,
        "due_at": iso(d.due_at),
        "tz": d.tz,
        "date_only": d.date_only,
        "local": when.iso_local(d.due_at, d.tz),
        "passed": d.due_at < now,
        "seconds_left": _seconds_left(d, now),
        "answer": d.answer,
        "answered_at": iso(d.answered_at),
        "changes": list(d.changes or []),
    }


def next_date_dict(d: ProjectDate | None, now: datetime) -> dict | None:
    if d is None:
        return None
    return {
        "id": d.id,
        "kind": d.kind,
        "label": d.label,
        "due_at": iso(d.due_at),
        "tz": d.tz,
        "date_only": d.date_only,
        # The venue's own day and time. 23:59 AoE is already the next day in UTC.
        "local": when.iso_local(d.due_at, d.tz),
        "seconds_left": _seconds_left(d, now),
    }


def version_dict(v: Version, created_by: User | None) -> dict:
    return {
        "id": v.id,
        "number": v.number,
        "kind": v.kind,
        "label": v.label,
        "tag": v.tag,
        "commit": v.commit,
        "date_id": v.date_id,
        "note": v.note,
        "pdf_name": v.pdf_name if v.pdf_sha else None,
        "created_at": iso(v.created_at),
        "created_by": person(created_by),
        "compiled": _compiled(v),
    }


def _compiled(v: Version) -> dict | None:
    """The version's own compile (§14), looked up here so that every place
    that sends a version sends it."""
    session = object_session(v)
    row = session.get(Compile, v.compile_id) if session is not None and v.compile_id else None
    if row is None:
        return None
    return {
        "id": row.id,
        "status": compile_status(row),
        "pdf_url": _compile_url(row, "pdf") if row.pdf_sha else None,
        "log_url": _compile_url(row, "log"),
    }


def compile_status(c: Compile) -> str:
    """A compile that is still "running" long after any compile could have
    ended was cut off by a restart of the server, and is reported as
    unavailable."""
    settings = get_settings()
    limit = timedelta(seconds=4 * settings.compile_timeout_seconds + 600)
    if c.status == "running" and c.created_at < utcnow() - limit:
        return "unavailable"
    return c.status


def _compile_url(c: Compile, what: str) -> str:
    return f"/api/projects/{c.project_id}/compiles/{c.id}/{what}"


def compile_dict(c: Compile) -> dict:
    """``pdf_url`` and ``log_url`` are paths on this server."""
    return {
        "id": c.id,
        "kind": c.kind,
        "status": compile_status(c),
        "ref": c.ref,
        "commit": c.commit,
        "compiler": c.compiler,
        "main_file": c.main_file,
        "duration_ms": c.duration_ms,
        "created_at": iso(c.created_at),
        "problems": list(c.problems or []),
        "pdf_url": _compile_url(c, "pdf") if c.pdf_sha else None,
        "log_url": _compile_url(c, "log"),
    }


def member_dict(m: Membership, user: User) -> dict:
    return {
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "role": m.role,
        "added_at": iso(m.added_at),
    }


def summary(
    project: Project,
    membership: Membership,
    folder_path: str,
    dates: list[ProjectDate],
    has_submission_version: bool,
    now: datetime,
    writing_days: int = 30,
    open_threads: int = 0,
) -> dict:
    """The list's view of a project, from the caller's side: their role, where
    they put it and their usual writing time. ``folder_path`` is "" at the top
    level."""
    submitted = lifecycle.handed_in(dates, has_submission_version)
    window = lifecycle.writing_window(project, dates, submitted, writing_days)
    return {
        "id": project.id,
        "name": project.name,
        "title": project.title,
        "paper": project.paper,
        "conference": project.conference,
        "year": project.year,
        "track": project.track,
        "outcome": project.outcome,
        "labels": list(project.labels or []),
        "archived": project.archived,
        "planned": project.planned,
        "role": membership.role,
        "folder_id": membership.folder_id,
        "folder_path": folder_path,
        "next_date": next_date_dict(lifecycle.next_date(project, dates, now), now),
        "question_count": sum(1 for d in dates if lifecycle.asks(project, d, submitted, now)),
        "open_threads": open_threads,
        "writing_days": project.writing_days or writing_days,
        # The paper's own writing time, null when it uses the owner's usual one.
        "own_writing_days": project.writing_days,
        "writing": _writing_dict(project, window),
        # The caller muted the reminder mails for this paper (§5, Reminders).
        "reminders_muted": bool(membership.reminders_muted),
        "updated_at": iso(project.updated_at),
        "url": project_url(project.name),
    }


def _writing_dict(project: Project, window: tuple | None) -> dict | None:
    if window is None:
        return None
    days, target, start = window
    return {
        "days": days,
        "own": project.writing_days is not None,
        "start": iso(start),
        "end": iso(target.due_at),
        "date_id": target.id,
    }


def detail_fields(project: Project) -> dict:
    """Every field of §4 that the summary leaves out."""
    out = {field: getattr(project, field) for field in DETAIL_FIELDS}
    out["outcome_at"] = iso(project.outcome_at)
    out["created_at"] = iso(project.created_at)
    return out


# --- Sharing the timeline (§3) -------------------------------------------------------

# What a shared timeline shows of a paper and of a date. Not the labels, the
# folder, your role or the questions, and not who moved a date.
SHARED_PROJECT_FIELDS = (
    "id", "name", "title", "conference", "year", "track", "outcome", "planned",
    "writing_days", "writing", "next_date",
)
SHARED_DATE_FIELDS = (
    "id", "kind", "label", "due_at", "tz", "date_only", "local", "passed", "seconds_left", "answer",
)


def timeline_share_dict(share: TimelineShare, folder_path: str | None) -> dict:
    return {
        "token": share.token,
        "url": f"{get_settings().base_url.rstrip('/')}/timeline/{share.token}",
        "folder_id": share.folder_id,
        "folder": folder_path,
        "created_at": iso(share.created_at),
    }


def shared_project(summary: dict) -> dict:
    """A timeline row (a summary with its dates) cut down to what the chart draws."""
    out = {field: summary[field] for field in SHARED_PROJECT_FIELDS}
    out["dates"] = [
        {
            **{field: d[field] for field in SHARED_DATE_FIELDS},
            "changes": [{k: c.get(k) for k in ("at", "from", "to")} for c in d["changes"]],
        }
        for d in summary["dates"]
    ]
    return out
