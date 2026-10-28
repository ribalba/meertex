"""Reviews, GitHub's pull request review for a paper (docs/DESIGN.md §13).

A review is one reviewer's threads on a fixed state of the files (a commit),
with a verdict once it is submitted. A thread is a conversation about a span of
one file. It can also stand on its own, outside any review (a single comment).
Threads are anchored to a snapshot and found in the live text again on every
request (app/anchors.py), so no stored offset has to follow the edits.

A pending review and its threads exist only for their reviewer. Every lookup
below filters on that, so for anybody else they are 404, and they never make a
thread id prefix ambiguous.

State changes lock the thread's row, so two people resolving at once cannot
both win. Snapshots store the live document through their own database
session. That never waits for a thread row, so taking one while the row is
locked is safe.

Mails go out after the database commit, as background tasks, so a slow mail
server never holds a request and a write that failed never mails anybody.
Whoever a comment, a note or a summary @-mentions (app/mentions.py) is found
when it is written and stored with it.
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Query as OrmQuery
from sqlalchemy.orm import Session as DBSession

from .. import emailer, live, mentions
from ..access import resolve_project
from ..anchors import (
    Anchorer,
    Span,
    follow_path,
    line_span,
    lines_of,
    read_live,
    word_diff,
)
from ..config import get_settings
from ..database import DB
from ..live import git, rooms, ydoc
from ..models import (
    VERDICTS,
    Comment,
    Membership,
    Project,
    Review,
    Thread,
    User,
    Version,
    utcnow,
)
from ..review_serialize import People, review_dict, thread_dict, thread_people
from ..security import SessionOrTokenUser
from ..serialize import project_url
from .files import call_live

router = APIRouter(prefix="/api", tags=["reviews"])

# Ids may be shortened to a prefix of at least this many characters.
MIN_PREFIX = 8
MAX_BODY = 50_000
MAX_SUGGESTION = 500_000
APPLIED_NOTE = "Applied the suggestion"
_ID_RE = re.compile(r"^[0-9a-f-]{1,36}$")

StateFilter = Literal["open", "resolved", "accepted", "all"]
Verdict = Literal[VERDICTS]


class ReviewIn(BaseModel):
    # HEAD (the default), a version's tag, a commit, or live (a snapshot now).
    ref: str | None = Field(default=None, max_length=200)


class SubmitIn(BaseModel):
    verdict: Verdict
    summary: str | None = Field(default=None, max_length=MAX_BODY)


class ThreadIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    # What the offsets refer to: live (the default), HEAD, a tag or a commit.
    ref: str | None = Field(default=None, max_length=200)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    body: str = Field(default="", max_length=MAX_BODY)
    suggestion: str | None = Field(default=None, max_length=MAX_SUGGESTION)
    review_id: str | None = Field(default=None, max_length=36)


class CommentIn(BaseModel):
    body: str = Field(default="", max_length=MAX_BODY)
    suggestion: str | None = Field(default=None, max_length=MAX_SUGGESTION)


class CommentPatch(BaseModel):
    body: str | None = Field(default=None, max_length=MAX_BODY)
    # null removes the suggestion.
    suggestion: str | None = Field(default=None, max_length=MAX_SUGGESTION)


class NoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=MAX_BODY)


class ApplyIn(BaseModel):
    # The comment whose suggestion to apply. The newest suggestion when omitted.
    comment_id: str | None = Field(default=None, max_length=36)


def new_id() -> str:
    return str(uuid.uuid4())


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


# --- Finding things ------------------------------------------------------------------


def _review_visible(user: User):
    return or_(Review.state == "submitted", Review.reviewer_id == user.id)


def _thread_visible(user: User):
    """Threads the user may see: single comments, threads of submitted
    reviews, and the threads of their own pending review. Needs Review
    outer-joined on the thread."""
    return or_(Thread.review_id.is_(None), Review.state == "submitted", Review.reviewer_id == user.id)


def _thread_query(db: DBSession, user: User) -> OrmQuery:
    return db.query(Thread).outerjoin(Review, Review.id == Thread.review_id).filter(_thread_visible(user))


def _by_id(query: OrmQuery, column, value: str, what: str):
    """The one row whose id is ``value`` or starts with it (at least
    MIN_PREFIX characters). 404 when none, 409 with the candidates when
    several do."""
    value = value.strip().lower()
    if not _ID_RE.match(value):
        raise HTTPException(status_code=404, detail=f"{what.capitalize()} not found")
    if len(value) < MIN_PREFIX:
        raise unprocessable(f"Give at least {MIN_PREFIX} characters of the {what} id")
    rows = query.filter(column.like(value + "%")).order_by(column).limit(20).all()
    exact = [r for r in rows if r.id == value]
    if exact:
        return exact[0]
    if not rows:
        raise HTTPException(status_code=404, detail=f"{what.capitalize()} not found")
    if len(rows) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"More than one {what} id starts with {value}. Give more of it.",
                "candidates": [r.id for r in rows],
            },
        )
    return rows[0]


def _find_review(db: DBSession, user: User, project: Project, review_id: str) -> Review:
    query = db.query(Review).filter(Review.project_id == project.id, _review_visible(user))
    return _by_id(query, Review.id, review_id, "review")


def _find_thread(db: DBSession, user: User, project: Project, thread_id: str, lock: bool = False) -> Thread:
    query = _thread_query(db, user).filter(Thread.project_id == project.id)
    thread = _by_id(query, Thread.id, thread_id, "thread")
    if lock:
        # Again by its full id, waiting for whoever changes it right now, and
        # with the state they left behind.
        thread = db.query(Thread).filter(Thread.id == thread.id).with_for_update().populate_existing().one()
    return thread


def _find_comment(db: DBSession, user: User, project: Project, comment_id: str) -> tuple[Comment, Thread]:
    query = (
        db.query(Comment)
        .join(Thread, Thread.id == Comment.thread_id)
        .outerjoin(Review, Review.id == Thread.review_id)
        .filter(Thread.project_id == project.id, _thread_visible(user))
    )
    comment = _by_id(query, Comment.id, comment_id, "comment")
    return comment, db.get(Thread, comment.thread_id)


def _pending_review(db: DBSession, project: Project, user: User) -> Review | None:
    return (
        db.query(Review)
        .filter(Review.project_id == project.id, Review.reviewer_id == user.id, Review.state == "pending")
        .order_by(Review.created_at)
        .first()
    )


def _not_pending(db: DBSession, thread: Thread) -> None:
    if thread.review_id:
        review = db.get(Review, thread.review_id)
        if review is not None and review.state == "pending":
            raise unprocessable("This thread belongs to a review that is not submitted yet")


def _version_for(db: DBSession, project: Project, ref: str, commit: str) -> Version | None:
    """The version a review's state is. That is the one whose tag was given,
    else the newest one at that commit."""
    versions = (
        db.query(Version)
        .filter(Version.project_id == project.id, or_(Version.tag == ref, Version.commit == commit))
        .order_by(Version.number.desc())
        .all()
    )
    tagged = [v for v in versions if v.tag == ref]
    return (tagged or versions or [None])[0]


# --- Responses -----------------------------------------------------------------------


def _counts(db: DBSession, review_ids: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(dict)
    if review_ids:
        rows = (
            db.query(Thread.review_id, Thread.state, func.count())
            .filter(Thread.review_id.in_(review_ids))
            .group_by(Thread.review_id, Thread.state)
            .all()
        )
        for review_id, state, n in rows:
            out[review_id][state] = n
    return out


def _reviews_out(db: DBSession, reviews: list[Review]) -> list[dict]:
    counts = _counts(db, [r.id for r in reviews])
    version_ids = {r.version_id for r in reviews if r.version_id}
    versions = {v.id: v for v in db.query(Version).filter(Version.id.in_(version_ids)).all()} if version_ids else {}
    people = People(db, (i for r in reviews for i in (r.reviewer_id, *(r.mentions or []))))
    return [review_dict(r, people, versions.get(r.version_id or ""), counts[r.id]) for r in reviews]


def _threads_out(db: DBSession, threads: list[Thread], projects: dict[str, Project] | None = None) -> list[dict]:
    """Threads with their current spans and comments, in file order (and by
    project name first when ``projects`` is given)."""
    if not threads:
        return []
    comments: dict[str, list[Comment]] = defaultdict(list)
    rows = (
        db.query(Comment)
        .filter(Comment.thread_id.in_([t.id for t in threads]))
        .order_by(Comment.created_at, Comment.id)
        .all()
    )
    for c in rows:
        comments[c.thread_id].append(c)
    review_ids = {t.review_id for t in threads if t.review_id}
    reviews = {r.id: r for r in db.query(Review).filter(Review.id.in_(review_ids)).all()} if review_ids else {}
    people = People(db, thread_people(threads, rows))
    anchorer = Anchorer()
    located = [(t, anchorer.locate(t)) for t in threads]

    def order(item: tuple[Thread, Span]) -> tuple:
        t, span = item
        project = projects.get(t.project_id) if projects else None
        return (project.name if project else "", t.project_id, span.path, span.start, span.end, t.created_at)

    located.sort(key=order)
    return [
        thread_dict(t, span, comments[t.id], people, reviews.get(t.review_id or ""),
                    projects.get(t.project_id) if projects else None)
        for t, span in located
    ]


def _thread_out(db: DBSession, thread: Thread) -> dict:
    return _threads_out(db, [thread])[0]


# --- Mail ----------------------------------------------------------------------------


def _name(user: User) -> str:
    return user.name or user.email


def _thread_link(project: Project, thread: Thread) -> str:
    if thread.review_id:
        return f"{project_url(project.name)}/reviews/{thread.review_id}#thread-{thread.id}"
    return f"{project_url(project.name)}/reviews#thread-{thread.id}"


def _where(out: dict) -> str:
    first, last = out["line_start"], out["line_end"]
    lines = f"line {first}" if first == last else f"lines {first} to {last}"
    return f"{out['path']}, {lines}"


def _members(
    db: DBSession, project: Project, user_ids: set[str | None] | None = None, roles: tuple[str, ...] | None = None
) -> list[User]:
    """The project's members among ``user_ids``, or with one of ``roles``.
    Somebody who left never hears about the project again."""
    query = db.query(User).join(Membership, Membership.user_id == User.id).filter(Membership.project_id == project.id)
    if roles is not None:
        query = query.filter(Membership.role.in_(roles))
    if user_ids is not None:
        query = query.filter(User.id.in_({i for i in user_ids if i}))
    return query.order_by(User.email).all()


def _mail_thread(
    background: BackgroundTasks,
    db: DBSession,
    project: Project,
    thread: Thread,
    out: dict,
    actor: User,
    event: str,
    text: str,
    mentioned: Iterable[str] = (),
) -> None:
    """Tell everybody who wrote in the thread, except ``actor``. A new single
    comment goes to the owner and the editors instead, who would otherwise
    never hear of it, and an edit only to the people it newly mentions. The
    people in ``mentioned`` get a mail that says they were mentioned, in place
    of the other one, unless they turned those mails off."""
    if out["pending"]:
        return
    if event == "comment":
        recipients = _members(db, project, roles=("owner", "editor"))
    elif event == "edited":
        recipients = []
    else:
        writers = {c["author"]["id"] for c in out["comments"] if c["author"]} | {thread.created_by}
        recipients = _members(db, project, writers)
    wanted = set(mentioned) - {actor.id}
    tagged = [u for u in _members(db, project, wanted) if u.mention_mails] if wanted else []
    link = _thread_link(project, thread)
    title = project.title or project.name
    for person in tagged:
        background.add_task(
            emailer.send_mention, person.email, _name(actor), title, event, _where(out), thread.quote, text, link
        )
    told = {u.id for u in tagged}
    for person in recipients:
        if person.id != actor.id and person.id not in told:
            background.add_task(
                emailer.send_thread_event, person.email, _name(actor), title, event, _where(out), thread.quote, text, link
            )


# --- Reviews -------------------------------------------------------------------------


@router.get("/projects/{ref}/reviews")
def list_reviews(ref: str, db: DB, user: SessionOrTokenUser) -> list[dict]:
    """The submitted reviews and your own pending one, newest first."""
    project, _ = resolve_project(db, user, ref)
    reviews = db.query(Review).filter(Review.project_id == project.id, _review_visible(user)).all()
    reviews.sort(key=lambda r: r.submitted_at or r.created_at, reverse=True)
    return _reviews_out(db, reviews)


@router.post("/projects/{ref}/reviews", status_code=201)
def start_review(ref: str, response: Response, db: DB, user: SessionOrTokenUser, body: ReviewIn | None = None) -> dict:
    """201 with a new pending review, or 200 with the one you have already."""
    project, _ = resolve_project(db, user, ref, "reviewer")
    pending = _pending_review(db, project, user)
    if pending is None:
        at = (body.ref if body and body.ref else "HEAD").strip()
        commit = call_live(live.commit_of, project.id, at)
        # One pending review per person and project. The membership row
        # serialises two starts at the same moment.
        db.query(Membership).filter(
            Membership.project_id == project.id, Membership.user_id == user.id
        ).with_for_update().one()
        pending = _pending_review(db, project, user)
        if pending is None:
            version = _version_for(db, project, at, commit)
            review = Review(
                id=new_id(),
                project_id=project.id,
                reviewer_id=user.id,
                commit=commit,
                version_id=version.id if version else None,
                state="pending",
                summary="",
            )
            db.add(review)
            db.commit()
            return _reviews_out(db, [review])[0]
    response.status_code = 200
    return _reviews_out(db, [pending])[0]


@router.get("/projects/{ref}/reviews/{review_id}")
def get_review(ref: str, review_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    """The review and its threads."""
    project, _ = resolve_project(db, user, ref)
    review = _find_review(db, user, project, review_id)
    out = _reviews_out(db, [review])[0]
    out["threads"] = _threads_out(db, db.query(Thread).filter(Thread.review_id == review.id).all())
    return out


@router.post("/projects/{ref}/reviews/{review_id}/submit")
def submit_review(
    ref: str, review_id: str, body: SubmitIn, background: BackgroundTasks, db: DB, user: SessionOrTokenUser
) -> dict:
    project, _ = resolve_project(db, user, ref, "reviewer")
    review = _find_review(db, user, project, review_id)
    if review.reviewer_id != user.id:
        raise forbidden("Only the reviewer can submit a review")
    review = db.query(Review).filter(Review.id == review.id).with_for_update().populate_existing().one()
    if review.state != "pending":
        raise unprocessable("This review is submitted already")
    review.state = "submitted"
    review.verdict = body.verdict
    review.summary = (body.summary or "").strip()
    review.mentions = mentions.mentioned_ids(db, project, review.summary)
    review.submitted_at = utcnow()
    db.commit()

    out = _reviews_out(db, [review])[0]
    link = f"{project_url(project.name)}/reviews/{review.id}"
    threads = sum(out["counts"].values())
    title = project.title or project.name
    # Comments of a pending review mailed nobody, so their mentions are told now.
    written = (
        db.query(Comment.mentions)
        .join(Thread, Thread.id == Comment.thread_id)
        .filter(Thread.review_id == review.id, Comment.kind == "comment")
        .all()
    )
    mentioned = (set(review.mentions) | {i for (ids,) in written for i in ids or []}) - {user.id}
    authors = [u for u in _members(db, project, roles=("owner", "editor")) if u.id != user.id]
    for person in authors:
        background.add_task(
            emailer.send_review_submitted, person.email, _name(user), title,
            review.verdict, review.summary, threads, link, person.id in mentioned,
        )
    # The others it mentions get a mail of their own.
    others = mentioned - {u.id for u in authors}
    for person in _members(db, project, others) if others else []:
        if person.mention_mails:
            background.add_task(
                emailer.send_review_mention, person.email, _name(user), title, review.verdict, review.summary, link
            )
    return out


@router.delete("/projects/{ref}/reviews/{review_id}")
def discard_review(ref: str, review_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    """Throw a pending review away with its threads (they go with the row)."""
    project, _ = resolve_project(db, user, ref)
    review = _find_review(db, user, project, review_id)
    if review.reviewer_id != user.id:
        raise forbidden("Only the reviewer can discard a review")
    if review.state != "pending":
        raise unprocessable("A submitted review cannot be discarded")
    db.delete(review)
    db.commit()
    return {"ok": True}


@router.get("/projects/{ref}/reviews/{review_id}/changes")
def review_changes(ref: str, review_id: str, db: DB, user: SessionOrTokenUser, word: bool = True) -> dict:
    """The diff from the review's commit to live (§11 diff)."""
    project, _ = resolve_project(db, user, ref)
    review = _find_review(db, user, project, review_id)
    return call_live(live.diff, project.id, review.commit, "live", word)


# --- Threads -------------------------------------------------------------------------


@router.get("/projects/{ref}/threads")
def list_threads(
    ref: str,
    db: DB,
    user: SessionOrTokenUser,
    state: StateFilter = "open",
    review: Annotated[str | None, Query(description="a review id or a prefix of one")] = None,
    path: Annotated[str | None, Query(description="the file as it is called now")] = None,
) -> list[dict]:
    project, _ = resolve_project(db, user, ref)
    query = _thread_query(db, user).filter(Thread.project_id == project.id)
    if review:
        query = query.filter(Thread.review_id == _find_review(db, user, project, review).id)
    if state != "all":
        query = query.filter(Thread.state == state)
    out = _threads_out(db, query.all())
    return [t for t in out if t["path"] == path] if path else out


@router.get("/threads")
def list_all_threads(db: DB, user: SessionOrTokenUser, state: StateFilter = "open") -> list[dict]:
    """The threads of every project you are a member of, each with its project."""
    projects = {
        p.id: p
        for p in db.query(Project)
        .join(Membership, Membership.project_id == Project.id)
        .filter(Membership.user_id == user.id)
        .all()
    }
    if not projects:
        return []
    query = _thread_query(db, user).filter(Thread.project_id.in_(projects))
    if state != "all":
        query = query.filter(Thread.state == state)
    return _threads_out(db, query.all(), projects)


def _base(project: Project, path: str, ref: str) -> tuple[str, str]:
    """The commit a new thread is anchored to and the file's text there. live
    takes a snapshot first."""
    problem = ydoc.path_problem(path)
    if problem:
        raise unprocessable(problem)
    commit = call_live(live.commit_of, project.id, ref)
    data = git.show(project.id, commit, path)
    if data is None:
        raise HTTPException(status_code=404, detail=f"There is no file {path} at {ref}")
    if not ydoc.is_text(data, get_settings().max_text_bytes):
        raise unprocessable(f"{path} is not a text file, so it cannot be commented on")
    return commit, data.decode()


def _span(text: str, body: ThreadIn) -> tuple[int, int]:
    if body.start is not None or body.end is not None:
        if body.start is None or body.end is None:
            raise unprocessable("Give both start and end")
        if not body.start <= body.end <= len(text):
            raise unprocessable(f"start and end must lie within the file's {len(text)} characters, start first")
        return body.start, body.end
    if body.line_start is not None:
        line_end = body.line_end if body.line_end is not None else body.line_start
        if line_end < body.line_start:
            raise unprocessable("line_end must not come before line_start")
        try:
            return line_span(text, body.line_start, line_end)
        except ValueError as exc:
            raise unprocessable(str(exc)) from None
    raise unprocessable("Give start and end, or line_start and line_end")


def _file_id(project: Project, commit: str, path: str, at: str) -> str | None:
    """The live entry of the file a new thread is on. A file that was renamed
    since an older commit is followed through git's rename detection, up to a
    snapshot of now."""
    with rooms.get(project.id).read() as doc:
        file_id = read_live(doc).by_path.get(path)
    if file_id is not None or at == "live":
        return file_id
    now_path = follow_path(project.id, commit, call_live(live.snapshot, project.id), path)
    if now_path is None or now_path == path:
        return None
    with rooms.get(project.id).read() as doc:
        return read_live(doc).by_path.get(now_path)


def _content(body: str | None, suggestion: str | None) -> str:
    """The comment's text. An empty suggestion is one (delete the span), an
    empty body without one is nothing to say."""
    text = (body or "").strip()
    if not text and suggestion is None:
        raise unprocessable("A comment needs a body or a suggestion")
    return text


@router.post("/projects/{ref}/threads", status_code=201)
def create_thread(ref: str, body: ThreadIn, background: BackgroundTasks, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref, "reviewer")
    text = _content(body.body, body.suggestion)
    review = None
    if body.review_id:
        review = _find_review(db, user, project, body.review_id)
        if review.reviewer_id != user.id or review.state != "pending":
            raise unprocessable("A thread can only be added to your own pending review")
    at = (body.ref or "live").strip()
    commit, base = _base(project, body.path, at)
    start, end = _span(base, body)
    file_id = _file_id(project, commit, body.path, at)
    line_start, line_end = lines_of(base, start, end)
    now = utcnow()
    thread = Thread(
        id=new_id(),
        project_id=project.id,
        review_id=review.id if review else None,
        created_by=user.id,
        file_id=file_id,
        base_commit=commit,
        base_path=body.path,
        base_start=start,
        base_end=end,
        base_line_start=line_start,
        base_line_end=line_end,
        quote=base[start:end],
        state="open",
        created_at=now,
        updated_at=now,
    )
    db.add(thread)
    db.flush()
    tagged = mentions.mentioned_ids(db, project, text)
    db.add(Comment(id=new_id(), thread_id=thread.id, author_id=user.id, kind="comment", body=text,
                   suggestion=body.suggestion, mentions=tagged, created_at=now))
    db.commit()
    out = _thread_out(db, thread)
    _mail_thread(background, db, project, thread, out, user, "comment", text, tagged)
    return out


@router.get("/projects/{ref}/threads/{thread_id}")
def get_thread(ref: str, thread_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    project, _ = resolve_project(db, user, ref)
    return _thread_out(db, _find_thread(db, user, project, thread_id))


@router.post("/projects/{ref}/threads/{thread_id}/comments", status_code=201)
def reply(
    ref: str, thread_id: str, body: CommentIn, background: BackgroundTasks, db: DB, user: SessionOrTokenUser
) -> dict:
    """A reply. Returns the thread."""
    project, _ = resolve_project(db, user, ref, "reviewer")
    thread = _find_thread(db, user, project, thread_id)
    text = _content(body.body, body.suggestion)
    now = utcnow()
    tagged = mentions.mentioned_ids(db, project, text)
    db.add(Comment(id=new_id(), thread_id=thread.id, author_id=user.id, kind="comment", body=text,
                   suggestion=body.suggestion, mentions=tagged, created_at=now))
    thread.updated_at = now
    db.commit()
    out = _thread_out(db, thread)
    _mail_thread(background, db, project, thread, out, user, "reply", text, tagged)
    return out


@router.patch("/projects/{ref}/comments/{comment_id}")
def edit_comment(
    ref: str, comment_id: str, body: CommentPatch, background: BackgroundTasks, db: DB, user: SessionOrTokenUser
) -> dict:
    """Change your comment's body or suggestion. Returns the thread. Only the
    people the edit adds a mention of are mailed."""
    project, _ = resolve_project(db, user, ref, "reviewer")
    comment, thread = _find_comment(db, user, project, comment_id)
    if comment.author_id != user.id:
        raise forbidden("Only its author can edit a comment")
    if comment.kind != "comment":
        raise unprocessable(f"This is the {comment.kind} event, not a comment. Events cannot be edited.")
    data = body.model_dump(exclude_unset=True)
    text = _content(data.get("body", comment.body), data.get("suggestion", comment.suggestion))
    before = set(comment.mentions or [])
    comment.body = text
    comment.mentions = mentions.mentioned_ids(db, project, text)
    if "suggestion" in data:
        comment.suggestion = data["suggestion"]
    comment.edited_at = thread.updated_at = utcnow()
    db.commit()
    out = _thread_out(db, thread)
    added = [i for i in comment.mentions if i not in before]
    if added:
        _mail_thread(background, db, project, thread, out, user, "edited", text, added)
    return out


@router.delete("/projects/{ref}/comments/{comment_id}")
def delete_comment(ref: str, comment_id: str, db: DB, user: SessionOrTokenUser) -> dict:
    """Delete your comment. The thread goes with its last comment, and then
    ``thread`` is null."""
    project, _ = resolve_project(db, user, ref, "reviewer")
    comment, thread = _find_comment(db, user, project, comment_id)
    if comment.author_id != user.id:
        raise forbidden("Only its author can delete a comment")
    if comment.kind != "comment":
        raise unprocessable(f"This is the {comment.kind} event, not a comment. Events cannot be deleted.")
    db.delete(comment)
    db.flush()
    left = db.query(Comment.id).filter(Comment.thread_id == thread.id, Comment.kind == "comment").count()
    if not left:
        db.delete(thread)
        db.commit()
        return {"ok": True, "thread": None}
    thread.updated_at = utcnow()
    db.commit()
    return {"ok": True, "thread": _thread_out(db, thread)}


# --- Thread state ----------------------------------------------------------------------


def _resolve(
    db: DBSession, thread: Thread, user: User, commit: str, note: str, diff: str, kind: str, tagged: list[str]
) -> None:
    now = utcnow()
    thread.state = "resolved"
    thread.resolved_by = user.id
    thread.resolved_at = now
    thread.resolution_commit = commit
    thread.resolution_note = note
    thread.resolution_diff = diff
    thread.accepted_by = None
    thread.accepted_at = None
    thread.updated_at = now
    db.add(Comment(id=new_id(), thread_id=thread.id, author_id=user.id, kind=kind, body=note, mentions=tagged,
                   created_at=now))


def _changing(db: DBSession, user: User, ref: str, thread_id: str, role: str) -> tuple[Project, Membership, Thread]:
    """The project and the thread, locked, for a state change. Pending
    threads have no state to change yet."""
    project, membership = resolve_project(db, user, ref, role)
    thread = _find_thread(db, user, project, thread_id, lock=True)
    _not_pending(db, thread)
    return project, membership, thread


def _event(
    background: BackgroundTasks,
    db: DBSession,
    project: Project,
    thread: Thread,
    user: User,
    event: str,
    note: str,
    tagged: list[str],
) -> dict:
    """Commit, answer with the thread and mail everybody who wrote in it or
    whom the note mentions."""
    db.commit()
    out = _thread_out(db, thread)
    _mail_thread(background, db, project, thread, out, user, event, note, tagged)
    return out


@router.post("/projects/{ref}/threads/{thread_id}/resolve")
def resolve_thread(
    ref: str, thread_id: str, background: BackgroundTasks, db: DB, user: SessionOrTokenUser, body: NoteIn | None = None
) -> dict:
    """Snapshot, and record how the span changed since the thread was made."""
    project, _, thread = _changing(db, user, ref, thread_id, "editor")
    if thread.state != "open":
        raise unprocessable(f"The thread is {thread.state} already")
    commit = call_live(live.snapshot, project.id)
    span = Anchorer().locate(thread)
    note = ((body.note if body else None) or "").strip()
    tagged = mentions.mentioned_ids(db, project, note)
    diff = word_diff(thread.quote, span.text, thread.base_line_start, span.line_start)
    _resolve(db, thread, user, commit, note, diff, "resolved", tagged)
    return _event(background, db, project, thread, user, "resolved", note, tagged)


@router.post("/projects/{ref}/threads/{thread_id}/apply")
def apply_suggestion(
    ref: str, thread_id: str, background: BackgroundTasks, db: DB, user: SessionOrTokenUser, body: ApplyIn | None = None
) -> dict:
    """Write a suggestion over the thread's current span in the live document
    (a CRDT edit, so every editor and checkout gets it) and resolve."""
    project, _, thread = _changing(db, user, ref, thread_id, "editor")
    if thread.state != "open":
        raise unprocessable(f"The thread is {thread.state}. Reopen it to apply a suggestion.")
    candidates = (
        db.query(Comment)
        .filter(Comment.thread_id == thread.id, Comment.kind == "comment", Comment.suggestion.is_not(None))
        .order_by(Comment.created_at.desc(), Comment.id)
    )
    if body and body.comment_id:
        chosen = _by_id(candidates, Comment.id, body.comment_id, "suggestion")
    else:
        chosen = candidates.first()
        if chosen is None:
            raise unprocessable("This thread has no suggestion to apply")
    suggestion = chosen.suggestion or ""

    anchorer = Anchorer()
    # git first, outside the room's lock.
    anchorer.base_text(project.id, thread.base_commit, thread.base_path)
    with rooms.get(project.id).edit(user.email) as doc:
        # Found and written under the room's lock, so no edit slips in between.
        anchorer.use_live(project.id, read_live(doc))
        span = anchorer.locate(thread)
        if span.outdated or span.file_id is None:
            raise unprocessable("The text of this thread is gone, so the suggestion cannot be applied")
        current = ydoc.get_entry(doc, span.file_id)
        assert current is not None and current.text is not None
        ydoc.write_text(doc, span.file_id, current.text[: span.start] + suggestion + current.text[span.end :])
    commit = call_live(live.snapshot, project.id)
    diff = word_diff(thread.quote, suggestion, thread.base_line_start, span.line_start)
    _resolve(db, thread, user, commit, APPLIED_NOTE, diff, "applied", [])
    return _event(background, db, project, thread, user, "applied", APPLIED_NOTE, [])


@router.post("/projects/{ref}/threads/{thread_id}/accept")
def accept_thread(
    ref: str, thread_id: str, background: BackgroundTasks, db: DB, user: SessionOrTokenUser, body: NoteIn | None = None
) -> dict:
    """The thread's creator (or the owner) is content with the outcome."""
    project, membership, thread = _changing(db, user, ref, thread_id, "reviewer")
    if thread.created_by != user.id and membership.role != "owner":
        raise forbidden("Only the person who started the thread, or the owner, can accept it")
    if thread.state == "accepted":
        raise unprocessable("The thread is accepted already")
    if thread.state == "open":
        # An open thread has nothing to accept but a suggestion.
        suggested = db.query(Comment.id).filter(
            Comment.thread_id == thread.id, Comment.kind == "comment", Comment.suggestion.is_not(None)
        ).first()
        if suggested is None:
            raise unprocessable("This thread has no suggestion, so there is nothing to accept before it is resolved")
    now = utcnow()
    note = ((body.note if body else None) or "").strip()
    tagged = mentions.mentioned_ids(db, project, note)
    thread.state = "accepted"
    thread.accepted_by = user.id
    thread.accepted_at = now
    thread.updated_at = now
    db.add(Comment(id=new_id(), thread_id=thread.id, author_id=user.id, kind="accepted", body=note, mentions=tagged,
                   created_at=now))
    return _event(background, db, project, thread, user, "accepted", note, tagged)


@router.post("/projects/{ref}/threads/{thread_id}/reopen")
def reopen_thread(
    ref: str, thread_id: str, background: BackgroundTasks, db: DB, user: SessionOrTokenUser, body: NoteIn | None = None
) -> dict:
    project, _, thread = _changing(db, user, ref, thread_id, "reviewer")
    if thread.state == "open":
        raise unprocessable("The thread is open already")
    now = utcnow()
    note = ((body.note if body else None) or "").strip()
    tagged = mentions.mentioned_ids(db, project, note)
    thread.state = "open"
    thread.resolved_by = thread.resolved_at = thread.resolution_commit = None
    thread.resolution_note = thread.resolution_diff = ""
    thread.accepted_by = thread.accepted_at = None
    thread.updated_at = now
    db.add(Comment(id=new_id(), thread_id=thread.id, author_id=user.id, kind="reopened", body=note, mentions=tagged,
                   created_at=now))
    return _event(background, db, project, thread, user, "reopened", note, tagged)

