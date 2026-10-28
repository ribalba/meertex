"""Reviews, review requests, threads and comments as the API sends them
(docs/DESIGN.md §13, Objects).

The web app, the CLI and agents read these shapes, so each one is built here
and nowhere else, like app/serialize.py does for the rest of §11. A thread's
current span is not stored. The caller finds it with an Anchorer and passes it
in, because finding it needs the live document.
"""

from collections.abc import Iterable

from sqlalchemy.orm import Session as DBSession

from .anchors import Span
from .models import Comment, Project, Review, ReviewRequest, Thread, User, Version
from .serialize import iso, person


class People:
    """Users by id, loaded in one query for everything a response mentions."""

    def __init__(self, db: DBSession, ids: Iterable[str | None]) -> None:
        wanted = {i for i in ids if i}
        users = db.query(User).filter(User.id.in_(wanted)).all() if wanted else []
        self.users = {u.id: u for u in users}

    def get(self, user_id: str | None) -> dict | None:
        return person(self.users.get(user_id)) if user_id else None


def thread_people(threads: Iterable[Thread], comments: Iterable[Comment]) -> set[str | None]:
    """Every user id that the threads and their comments mention."""
    ids: set[str | None] = set()
    for t in threads:
        ids |= {t.created_by, t.resolved_by, t.accepted_by}
    for c in comments:
        ids |= {c.author_id, *(c.mentions or [])}
    return ids


def mentioned(ids: list[str] | None, people: People) -> list[dict]:
    """The people a comment or a summary mentions, without those gone since."""
    return [p for p in (people.get(i) for i in ids or []) if p]


def comment_dict(c: Comment, people: People) -> dict:
    return {
        "id": c.id,
        "author": people.get(c.author_id),
        "kind": c.kind,
        "body": c.body,
        "suggestion": c.suggestion,
        "mentions": mentioned(c.mentions, people),
        "created_at": iso(c.created_at),
        "edited_at": iso(c.edited_at),
    }


def thread_dict(
    t: Thread,
    span: Span,
    comments: list[Comment],
    people: People,
    review: Review | None,
    project: Project | None = None,
) -> dict:
    """``project`` is given for the list across projects, which names it."""
    resolution = None
    if t.state in ("resolved", "accepted") and t.resolved_at is not None:
        resolution = {
            "by": people.get(t.resolved_by),
            "at": iso(t.resolved_at),
            "commit": t.resolution_commit,
            "note": t.resolution_note,
            "diff": t.resolution_diff,
        }
    accepted = None
    if t.state == "accepted" and t.accepted_at is not None:
        accepted = {"by": people.get(t.accepted_by), "at": iso(t.accepted_at)}
    out = {
        "id": t.id,
        "review_id": t.review_id,
        "state": t.state,
        "pending": review is not None and review.state == "pending",
        "created_by": people.get(t.created_by),
        "created_at": iso(t.created_at),
        "updated_at": iso(t.updated_at),
        "file_id": span.file_id,
        "path": span.path,
        "start": span.start,
        "end": span.end,
        "start16": span.start16,
        "end16": span.end16,
        "line_start": span.line_start,
        "line_end": span.line_end,
        "text": span.text,
        "quote": t.quote,
        "changed": span.changed,
        "outdated": span.outdated,
        "base": {
            "commit": t.base_commit,
            "path": t.base_path,
            "start": t.base_start,
            "end": t.base_end,
            "line_start": t.base_line_start,
            "line_end": t.base_line_end,
        },
        "resolution": resolution,
        "accepted": accepted,
        "comments": [comment_dict(c, people) for c in comments],
    }
    if project is not None:
        out["project"] = {"id": project.id, "name": project.name, "title": project.title}
    return out


def _version(version: Version | None) -> dict | None:
    return {"id": version.id, "tag": version.tag, "label": version.label} if version else None


def request_dict(rq: ReviewRequest, people: People, version: Version | None) -> dict:
    return {
        "id": rq.id,
        "reviewer": people.get(rq.reviewer_id),
        "requested_by": people.get(rq.requested_by),
        "state": rq.state,
        "note": rq.note,
        "commit": rq.commit,
        "version": _version(version),
        "review_id": rq.review_id,
        "created_at": iso(rq.created_at),
        "done_at": iso(rq.done_at),
    }


def review_dict(r: Review, people: People, version: Version | None, counts: dict[str, int]) -> dict:
    return {
        "id": r.id,
        "reviewer": people.get(r.reviewer_id),
        "state": r.state,
        "verdict": r.verdict,
        "summary": r.summary,
        "mentions": mentioned(r.mentions, people),
        "commit": r.commit,
        "version": _version(version),
        "created_at": iso(r.created_at),
        "submitted_at": iso(r.submitted_at),
        "counts": {state: counts.get(state, 0) for state in ("open", "resolved", "accepted")},
    }
