"""Request bodies of the project API (DESIGN §3 to §7, §11).

Responses are plain dicts built in app/serialize.py, because their shapes
(next dates, questions, the caller's folder) depend on who asks and when.
Unknown fields in a body are ignored, so a client may send back what it got.
"""

from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from .models import COMPILERS, DATE_KINDS, OUTCOMES, VERSION_KINDS

DateKind = Literal[DATE_KINDS]
Outcome = Literal[OUTCOMES]
Compiler = Literal[COMPILERS]
VersionKind = Literal[VERSION_KINDS]
# Nobody can be added as, or made, the owner. There is exactly one.
GrantableRole = Literal["editor", "reviewer", "viewer"]

_URL = 2000


class ProjectFields(BaseModel):
    """The fields of §4 a client may set. ``None`` on a string clears it to ""."""

    title: str | None = Field(default=None, max_length=2000)
    paper: str | None = Field(default=None, max_length=200)
    conference: str | None = Field(default=None, max_length=100)
    conference_name: str | None = Field(default=None, max_length=500)
    year: int | None = Field(default=None, ge=1900, le=2999)
    track: str | None = Field(default=None, max_length=200)
    cfp_url: str | None = Field(default=None, max_length=_URL)
    conference_url: str | None = Field(default=None, max_length=_URL)
    submission_url: str | None = Field(default=None, max_length=_URL)
    location: str | None = Field(default=None, max_length=300)
    publisher: str | None = Field(default=None, max_length=200)
    page_limit: str | None = Field(default=None, max_length=500)
    template: str | None = Field(default=None, max_length=300)
    review_model: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=200_000)
    main_file: str | None = Field(default=None, max_length=500)
    # §14. None goes back to the default, pdflatex.
    compiler: Compiler | None = None
    planned: bool | None = None
    # Null on PATCH goes back to the owner's usual writing time (§5).
    writing_days: int | None = Field(default=None, ge=1, le=365)
    outcome: Outcome | None = None
    labels: list[str] | None = Field(default=None, max_length=100)
    archived: bool | None = None
    doi: str | None = Field(default=None, max_length=300)
    publication_url: str | None = Field(default=None, max_length=_URL)


class DateIn(BaseModel):
    kind: DateKind
    label: str | None = Field(default=None, max_length=200)
    due_at: str | None = Field(default=None, max_length=64)
    date: str | None = Field(default=None, max_length=32)
    tz: str | None = Field(default=None, max_length=64)


class DatePatch(BaseModel):
    kind: DateKind | None = None
    label: str | None = Field(default=None, max_length=200)
    due_at: str | None = Field(default=None, max_length=64)
    date: str | None = Field(default=None, max_length=32)
    tz: str | None = Field(default=None, max_length=64)


class ProjectCreate(ProjectFields):
    # Overrides the naming pattern.
    name: str | None = Field(default=None, max_length=120)
    # A folder id or a path like "2027/Submitted", created when missing.
    folder: str | None = Field(default=None, max_length=2000)
    dates: list[DateIn] = Field(default_factory=list, max_length=100)
    # The files the project starts with. "template" is the venue's LaTeX
    # class (§4), which says nothing about the files.
    starter: Literal["empty", "basic"] = "empty"
    dry_run: bool = False


class StartIn(BaseModel):
    """Turning a plan into a paper (§4, Planned papers)."""

    title: str = Field(min_length=1, max_length=2000)
    paper: str | None = Field(default=None, max_length=200)
    starter: Literal["empty", "basic"] = "empty"
    keep_name: bool = False


class ProjectPatch(ProjectFields):
    # A rename. The checkouts keep their folder.
    name: str | None = Field(default=None, max_length=120)


class PlacementIn(BaseModel):
    folder_id: str | None = None
    position: float | None = None


class RemindersIn(BaseModel):
    """PUT /api/projects/{ref}/reminders: your own reminder mails for this paper (§5)."""

    muted: bool


class FolderCreate(BaseModel):
    name: str = Field(max_length=200)
    parent_id: str | None = None
    # A path, created when missing.
    parent: str | None = Field(default=None, max_length=2000)


class FolderPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    parent_id: str | None = None
    position: float | None = None


class TimelineShareIn(BaseModel):
    """POST and PATCH /api/timeline/share (§3, Sharing the timeline)."""

    # A folder id or path. Null or "" shares every paper.
    folder: str | None = Field(default=None, max_length=2000)


class MemberIn(BaseModel):
    email: EmailStr
    role: GrantableRole


class MemberPatch(BaseModel):
    role: GrantableRole


class VersionOptions(BaseModel):
    """``version`` of an answer: which state to freeze, and a note."""

    at: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=20_000)


class AnswerIn(BaseModel):
    answer: str = Field(max_length=30)
    # The new date of a "moved" answer, as in DateIn.
    due_at: str | None = Field(default=None, max_length=64)
    date: str | None = Field(default=None, max_length=32)
    tz: str | None = Field(default=None, max_length=64)
    version: VersionOptions | None = None


class VersionCreate(BaseModel):
    kind: VersionKind
    label: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=20_000)
    date_id: str | None = None
    # "now" (the default), "deadline", an ISO time or a commit.
    at: str | None = Field(default=None, max_length=200)


class VersionPatch(BaseModel):
    label: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=20_000)
