"""Where a paper stands: its next date and the questions waiting (DESIGN §5, §6).

Nothing here is stored. Both are computed from the project's dates, its
outcome and whether it was handed in, every time a project goes out, so they
can never disagree with the data they come from.
"""

from datetime import datetime, timedelta

from . import when
from .models import Project, ProjectDate

DATE_LABELS = {
    "abstract": "Abstract registration",
    "submission": "Paper submission",
    "supplementary": "Supplementary material",
    "rebuttal_start": "Rebuttal opens",
    "rebuttal_end": "Rebuttal deadline",
    "notification": "Notification",
    "revision": "Revision due",
    "camera_ready": "Camera-ready",
    "conference_start": "Conference starts",
    "conference_end": "Conference ends",
    "other": "",
}

YES_NO_MOVED = ("yes", "no", "moved")

# kind: (what passed, the question, the answers it takes)
_QUESTIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "abstract": ("The abstract registration deadline", "Did you register the abstract?", YES_NO_MOVED),
    "submission": ("The paper submission deadline", "Did you hand it in?", YES_NO_MOVED),
    "supplementary": (
        "The supplementary material deadline",
        "Did you hand in the supplementary material?",
        YES_NO_MOVED,
    ),
    "rebuttal_end": ("The rebuttal deadline", "Did you send a rebuttal?", YES_NO_MOVED),
    "notification": ("The notification date", "Was the paper accepted?", ("accepted", "rejected", "revision", "moved")),
    "revision": ("The revision deadline", "Did you hand in the revision?", YES_NO_MOVED),
    "camera_ready": ("The camera-ready deadline", "Did you hand in the camera-ready version?", ("yes", "moved")),
    "other": ("", "Is it done?", YES_NO_MOVED),
}

# Answering "yes" to these freezes a version of this kind (§7).
VERSION_FOR_YES = {
    "submission": "submission",
    "rebuttal_end": "rebuttal",
    "revision": "revision",
    "camera_ready": "camera_ready",
}

# After these outcomes the paper is out of the race: no next date, no questions.
CLOSED = frozenset({"rejected", "withdrawn", "not_submitted"})
# Asked only after the paper was handed in.
_AFTER_HANDING_IN = frozenset({"rebuttal_end", "notification", "revision"})
# About handing in. Once a decision is there, they are moot.
_BEFORE_DECISION = frozenset({"abstract", "submission", "supplementary", "rebuttal_end"})


def answers_for(kind: str) -> tuple[str, ...]:
    """The answers a date of this kind takes. Empty for kinds without a question."""
    entry = _QUESTIONS.get(kind)
    return entry[2] if entry else ()


def handed_in(dates: list[ProjectDate], has_submission_version: bool) -> bool:
    """Whether the paper went out: a submission date answered yes, a submission
    version, or no submission date to wait for at all."""
    submissions = [d for d in dates if d.kind == "submission"]
    if not submissions or has_submission_version:
        return True
    return any(d.answer == "yes" for d in submissions)


def _counts_for_outcome(kind: str, outcome: str | None) -> bool:
    """Dates that only matter on one branch of the review."""
    if kind == "camera_ready":
        return outcome == "accepted"
    if kind == "revision":
        return outcome == "revision"
    return True


def asks(project: Project, d: ProjectDate, submitted: bool, now: datetime) -> bool:
    """Whether date ``d`` has a question waiting. ``submitted`` is handed_in().
    A plan asks nothing: there is no paper yet to hand in."""
    if project.planned:
        return False
    if d.answer is not None or d.due_at >= now or d.kind not in _QUESTIONS:
        return False
    outcome = project.outcome
    if outcome in CLOSED:
        return False
    if not _counts_for_outcome(d.kind, outcome):
        return False
    if d.kind == "notification" and outcome is not None:
        return False
    if d.kind in _BEFORE_DECISION and outcome is not None:
        return False
    return not (d.kind in _AFTER_HANDING_IN and not submitted)


def question_text(d: ProjectDate) -> str:
    subject, question, _answers = _QUESTIONS[d.kind]
    if not subject:
        subject = f'The date "{d.label or "Other"}"'
    return f"{subject} passed on {when.describe(d.due_at, d.tz, d.date_only)}. {question}"


def questions(project: Project, dates: list[ProjectDate], has_submission_version: bool, now: datetime) -> list[dict]:
    """The project's waiting questions, oldest date first, as the API sends them."""
    submitted = handed_in(dates, has_submission_version)
    out = []
    for d in sorted(dates, key=lambda d: (d.due_at, d.position)):
        if not asks(project, d, submitted, now):
            continue
        out.append(
            {
                "project_id": project.id,
                "project_name": project.name,
                "date_id": d.id,
                "kind": d.kind,
                "label": d.label,
                "due_at": when.iso(d.due_at),
                "text": question_text(d),
                "answers": list(answers_for(d.kind)),
            }
        )
    return out


def dates_ahead(project: Project, dates: list[ProjectDate], now: datetime) -> list[ProjectDate]:
    """The unanswered dates still ahead that can matter, in no particular order.
    The next date is the first of them, and reminders (§5) mention them."""
    if project.outcome in CLOSED:
        return []
    return [
        d
        for d in dates
        if d.due_at >= now and d.answer is None and _counts_for_outcome(d.kind, project.outcome)
    ]


def next_date(project: Project, dates: list[ProjectDate], now: datetime) -> ProjectDate | None:
    """The earliest unanswered date still ahead that can matter, or None."""
    return min(dates_ahead(project, dates, now), key=lambda d: (d.due_at, d.position), default=None)


def writing_window(
    project: Project, dates: list[ProjectDate], submitted: bool, default_days: int
) -> tuple[int, ProjectDate, datetime] | None:
    """(days, the date the paper is due at, when writing starts), or None
    (DESIGN §5, Writing time). The paper is due at the next unanswered
    submission date while it was not handed in, and at the next unanswered
    revision date while the outcome is a revision."""
    if project.outcome is None and not submitted:
        kind = "submission"
    elif project.outcome == "revision":
        kind = "revision"
    else:
        return None
    due = [d for d in dates if d.kind == kind and d.answer is None]
    if not due:
        return None
    target = min(due, key=lambda d: (d.due_at, d.position))
    days = project.writing_days or default_days
    return days, target, target.due_at - timedelta(days=days)
