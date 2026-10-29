"""SQLAlchemy ORM models. docs/DESIGN.md has the full contract.

* Ids are UUID4 strings, naive UTC datetimes throughout (meerpad's conventions).
* JSON columns are replaced, never mutated in place: SQLAlchemy does not notice
  ``project.labels.append(x)``. Always assign a new list or dict.
* Strings that the design calls "empty by default" are NOT NULL with "".
* Everything that belongs to a project goes with it (ON DELETE CASCADE).
"""

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .config import get_settings
from .database import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    # Naive UTC throughout, normalised at the boundaries (meerpad's convention).
    return datetime.now(UTC).replace(tzinfo=None)


def _default_pattern() -> str:
    return get_settings().default_naming_pattern


# Days before a date that the reminder mail mentions it. 0 is the day itself.
DEFAULT_REMINDER_DAYS = (14, 3, 1, 0)


# --- Auth (meerpad's tables) -------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    # None until the web app first loads for this account and sends the
    # browser's zone (app/static/js/app.js). Read as UTC until then.
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Secret for the token API: scripts, the CLI, agents.
    api_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    # How new projects are named (DESIGN §4).
    naming_pattern: Mapped[str] = mapped_column(String(200), default=_default_pattern, nullable=False)
    # Days it usually takes to write a paper, for the timeline (DESIGN §5).
    writing_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30", nullable=False)
    # The daily reminder mail (DESIGN §5, Reminders): on or off, how many days
    # before a date it mentions it (0 is the day itself), and the hour of the
    # user's own day when it goes out.
    reminders: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    reminder_days: Mapped[list] = mapped_column(
        JSONType, default=lambda: list(DEFAULT_REMINDER_DAYS), server_default="[14, 3, 1, 0]", nullable=False
    )
    reminder_hour: Mapped[int] = mapped_column(Integer, default=8, server_default="8", nullable=False)
    # When the last reminder mail was put together, whether it said anything or not.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime)
    # A mail when somebody @-mentions this user in a comment (DESIGN §13, Mentions).
    mention_mails: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    # Spelling and grammar (DESIGN §17): on or off, the language, the personal
    # dictionary and the rules switched off. Its shape and validation are in
    # app/grammar/checker.py. {} reads as the defaults.
    grammar: Mapped[dict] = mapped_column(JSONType, default=dict, server_default="{}", nullable=False)
    # The profile picture's file name in DATA_DIR/avatars (app/avatars.py).
    # None shows the initials.
    avatar: Mapped[str | None] = mapped_column(String(80))
    # The first sign-in. An invite makes the account before that, so None is an
    # invited person who has not signed in yet (DESIGN §2, Accounts).
    first_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def avatar_url(self) -> str | None:
        return f"/api/avatars/{self.avatar}" if self.avatar else None

    @property
    def instance_admin(self) -> bool:
        """INSTANCE_ADMINS names this account (DESIGN §18)."""
        return self.email in get_settings().instance_admin_emails


class LoginToken(Base):
    """Magic link token emailed to a user, reusable until it expires (so mail
    scanners that prefetch the link do not burn it). An invite's token has no
    ``expires_at`` and never expires."""

    __tablename__ = "login_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Where to land after signing in (an invited project, for instance).
    redirect_to: Mapped[str | None] = mapped_column(String(500))


class LoginCode(Base):
    """Short sign-in code emailed alongside the link. One live code per email,
    only its HMAC stored, burned on success or after too many wrong guesses."""

    __tablename__ = "login_codes"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RateLimit(Base):
    """Fixed-window request counter, one row per ``action:subject`` key."""

    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ActiveDay(Base):
    """A UTC day on which the account used meertex: the web app, the CLI, the
    sync or git (DESIGN §18). One row per account and day, for the instance
    dashboard's active people."""

    __tablename__ = "active_days"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True, index=True)


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Folders (DESIGN §3) ---------------------------------------------------------


class Folder(Base):
    """A folder in one user's own tree. Names are unique among siblings, ignoring
    case (enforced in code, so a path like ``2027/Submitted`` is unambiguous)."""

    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class TimelineShare(Base):
    """A public, read-only link to one user's timeline (DESIGN §3, Sharing the
    timeline). One per user. A new link replaces the token, and deleting the
    row stops the sharing."""

    __tablename__ = "timeline_shares"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Only the papers in this folder and below it, null for all of them. Deleting
    # the folder ends the link instead of widening it to every paper.
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --- Projects (DESIGN §4) --------------------------------------------------------

OUTCOMES = ("accepted", "rejected", "revision", "withdrawn", "not_submitted")
COMPILERS = ("pdflatex", "xelatex", "lualatex")
ROLES = ("owner", "editor", "reviewer", "viewer")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_projects_owner_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), index=True, nullable=False)

    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    paper: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    conference: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    conference_name: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    track: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    cfp_url: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    conference_url: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    submission_url: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    location: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    publisher: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    page_limit: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    template: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    review_model: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    main_file: Mapped[str] = mapped_column(String(500), default="main.tex", nullable=False)

    # One of OUTCOMES, or null while nothing is decided.
    outcome: Mapped[str | None] = mapped_column(String(20), index=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime)
    labels: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    doi: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    publication_url: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    forked_from_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    # One of COMPILERS (§14).
    compiler: Mapped[str] = mapped_column(String(20), default="pdflatex", server_default="pdflatex", nullable=False)
    # A venue to hand something in to, before the paper's topic is clear (§4).
    planned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # This paper's writing time in days. Null means the owner's usual time (§5).
    writing_days: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Membership(Base):
    """A user's role on a project, and where the project sits in *their* folders."""

    __tablename__ = "memberships"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    # One of ROLES.
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"), index=True)
    position: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    added_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # This person gets no reminder mails about this project (DESIGN §5, Reminders).
    reminders_muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)


# --- Dates (DESIGN §5, §6) -------------------------------------------------------

DATE_KINDS = (
    "abstract",
    "submission",
    "supplementary",
    "rebuttal_start",
    "rebuttal_end",
    "notification",
    "revision",
    "camera_ready",
    "conference_start",
    "conference_end",
    "other",
)


class ProjectDate(Base):
    __tablename__ = "project_dates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # One of DATE_KINDS.
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # The instant, UTC. A date_only date is 23:59:59 of its day in `tz`.
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    tz: Mapped[str] = mapped_column(String(64), default="AoE", nullable=False)
    date_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The answer to this date's question (§6), null while unanswered.
    answer: Mapped[str | None] = mapped_column(String(30))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)
    answered_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Every move of due_at: [{at, by, from, to}], ISO strings.
    changes: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    position: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --- Reminders (DESIGN §5, Reminders) ----------------------------------------------

REMINDER_KINDS = ("date", "writing", "question")


class ReminderSent(Base):
    """One thing a reminder mail told one person, so that it is never told
    twice. A date that moves has a new ``due_at`` and reminds again."""

    __tablename__ = "reminders_sent"
    __table_args__ = (
        UniqueConstraint("user_id", "date_id", "kind", "threshold", "due_at", name="uq_reminders_sent"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date_id: Mapped[str] = mapped_column(ForeignKey("project_dates.id", ondelete="CASCADE"), index=True, nullable=False)
    # One of REMINDER_KINDS.
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # Days before the date for "date", the writing days for "writing", 0 for "question".
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    # The date's due_at for "date" and "question", when writing starts for "writing".
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --- Versions (DESIGN §7) --------------------------------------------------------

VERSION_KINDS = ("submission", "rebuttal", "revision", "camera_ready", "arxiv", "snapshot", "other")


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (
        UniqueConstraint("project_id", "tag", name="uq_versions_project_tag"),
        UniqueConstraint("project_id", "number", name="uq_versions_project_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    # One of VERSION_KINDS.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    # The git tag in the project's history, fixed once made.
    tag: Mapped[str] = mapped_column(String(120), nullable=False)
    commit: Mapped[str] = mapped_column(String(64), nullable=False)
    date_id: Mapped[str | None] = mapped_column(ForeignKey("project_dates.id", ondelete="SET NULL"))
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # The PDF that was handed in, as a project blob (§8).
    pdf_sha: Mapped[str | None] = mapped_column(String(64))
    pdf_name: Mapped[str | None] = mapped_column(String(300))
    # The compile of this version's commit (§14), started when it was made.
    compile_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


# --- The live document (DESIGN §8) ----------------------------------------------


class DocUpdate(Base):
    """One stored Yjs update of a project's live document. Loading a project
    applies them in id order. Compaction replaces many rows with one."""

    __tablename__ = "doc_updates"
    __table_args__ = (Index("ix_doc_updates_project_id_id", "project_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --- Compiling (DESIGN §14) -------------------------------------------------------

COMPILE_KINDS = ("pdf", "pdfdiff")
COMPILE_STATES = ("running", "success", "errors", "failure", "timeout", "unavailable")


class Compile(Base):
    """One compile of a project's files at a ref, or of a latexdiff between two.
    Reused for every request whose files hash the same (content_hash)."""

    __tablename__ = "compiles"
    __table_args__ = (Index("ix_compiles_project_hash", "project_id", "content_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # One of COMPILE_KINDS.
    kind: Mapped[str] = mapped_column(String(10), default="pdf", nullable=False)
    # The ref as asked for ("live", a tag, a commit, or "from..to" for a pdfdiff),
    # and the commit it named. Null for live.
    ref: Mapped[str] = mapped_column(String(300), default="live", nullable=False)
    commit: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler: Mapped[str] = mapped_column(String(20), nullable=False)
    main_file: Mapped[str] = mapped_column(String(500), nullable=False)
    # One of COMPILE_STATES.
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    log: Mapped[str] = mapped_column(Text, default="", nullable=False)
    problems: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    pdf_sha: Mapped[str | None] = mapped_column(String(64))
    # The compile service's job, for SyncTeX. It forgets old jobs.
    job_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


# --- Reviews (DESIGN §13) --------------------------------------------------------

REVIEW_STATES = ("pending", "submitted")
VERDICTS = ("approve", "comment", "changes_requested")
THREAD_STATES = ("open", "resolved", "accepted")
COMMENT_KINDS = ("comment", "resolved", "accepted", "reopened", "applied")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    # The snapshot under review, and the version it is, if it is one.
    commit: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("versions.id", ondelete="SET NULL"))
    # One of REVIEW_STATES. verdict is one of VERDICTS once submitted.
    state: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    verdict: Mapped[str | None] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # The ids of the members the summary @-mentions (§13, Mentions).
    mentions: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)


REQUEST_STATES = ("open", "done")


class ReviewRequest(Base):
    """An author asking a member to review the paper on a fixed state (§13,
    Requests). One open request per person and project. It is done when that
    person submits a review. Withdrawing deletes it."""

    __tablename__ = "review_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # The state to review, and the version it is, if it is one.
    commit: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("versions.id", ondelete="SET NULL"))
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # One of REQUEST_STATES. review_id is the review that answered it.
    state: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    review_id: Mapped[str | None] = mapped_column(ForeignKey("reviews.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)


class Thread(Base):
    """A conversation about a span of one file, anchored to a snapshot (the
    base) and mapped to the live text by diffing (§13, Anchoring)."""

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # Null for a single comment outside a review.
    review_id: Mapped[str | None] = mapped_column(ForeignKey("reviews.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # The live document's entry for the base path when the thread was made.
    file_id: Mapped[str | None] = mapped_column(String(64))
    base_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    base_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # [base_start, base_end) in code points of the file at base_commit.
    base_start: Mapped[int] = mapped_column(Integer, nullable=False)
    base_end: Mapped[int] = mapped_column(Integer, nullable=False)
    base_line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    base_line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # One of THREAD_STATES.
    state: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution_commit: Mapped[str | None] = mapped_column(String(64))
    resolution_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Word diff (git's porcelain format) between quote and the text at resolving.
    resolution_diff: Mapped[str] = mapped_column(Text, default="", nullable=False)
    accepted_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Comment(Base):
    """A comment in a thread, or an event (resolved, accepted, reopened,
    applied) that the thread's timeline shows between comments."""

    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True, nullable=False)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # One of COMMENT_KINDS.
    kind: Mapped[str] = mapped_column(String(20), default="comment", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Replacement text for the thread's span, or null.
    suggestion: Mapped[str | None] = mapped_column(Text)
    # The ids of the members the body @-mentions (§13, Mentions), found when
    # it is written, so an edit mails only the people it adds.
    mentions: Mapped[list] = mapped_column(JSONType, default=list, server_default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- References (DESIGN §16) -----------------------------------------------------

REFERENCE_RUN_STATES = ("running", "done", "failed")


class ReferenceRun(Base):
    """One check of a project's .bib entries, run in a thread of its own."""

    __tablename__ = "reference_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    # One of REFERENCE_RUN_STATES.
    status: Mapped[str] = mapped_column(String(10), default="running", nullable=False)
    claims: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    refresh: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Entries to look at and entries done, for the progress bar.
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ReferenceResult(Base):
    """What a run found for one entry, by the entry's fingerprint. An entry
    that changes gets a new fingerprint and is looked up again."""

    __tablename__ = "reference_results"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The key it had when it was looked up, for reading the table by hand.
    key: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # The record that matched, or null.
    record: Mapped[dict | None] = mapped_column(JSONType)
    findings: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # The claim findings and what they were asked about (sentences, source).
    claims: Mapped[list | None] = mapped_column(JSONType)
    claims_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    claims_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReferenceCache(Base):
    """An answer of doi.org, Crossref, Semantic Scholar and the others, shared
    by all projects. ``ok`` is false for a failure, which is kept briefly."""

    __tablename__ = "reference_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    value: Mapped[dict | None] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)
