"""Makes the example paper (docs/DESIGN.md §19).

The paper is made the way its people and the routes would have made it, only
with dates in the past. Its files go through the live document, and each
stage is a snapshot dated when it happened. The versions come from
services.create_version, the threads are anchored like the ones the review
routes make, and the reference check runs like a click on "Check references".

main.tex is the text as it was handed in, and refs.bib the bibliography of
that moment without its checked marks. The stages before and after come from
them by the swaps below. Each swap must find its text exactly once, so an
edit of main.tex that breaks one fails the tests and not a sign-in.
"""

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from .. import live, mentions, naming, services, tree, when
from ..anchors import Anchorer, lines_of, word_diff
from ..config import get_settings
from ..models import Comment, Membership, Project, ProjectDate, Review, ReviewRequest, Thread, User, utcnow
from ..project_schemas import DateIn
from ..references import bibtex
from ..references import service as references
from ..security import get_or_create_user
from . import AMARA, JONAS

logger = logging.getLogger("uvicorn.error")

HERE = Path(__file__).resolve().parent
NAME = "example-paper"
MAIN = "main.tex"
BIB = "refs.bib"

FIELDS = {
    "title": "The Whole Life of a Paper, Shown on an Example",
    "paper": "example",
    "conference": "WEP",
    "conference_name": "Workshop on Example Papers",
    "track": "Tool demonstrations",
    "location": "Online",
    "page_limit": "4 pages + references",
    "template": "article",
    "review_model": "double-blind",
    "labels": ["example"],
    "notes": (
        "This paper is an example, and its coauthors Jonas and Amara are made up. "
        "They never get a mail. Things to try:\n\n"
        "- Answer the question about the rebuttal on this page. Yes freezes the version Rebuttal.\n"
        "- Open the Editor, find the spelling mistake in Section 2 and watch the PDF compile.\n"
        "- On the Reviews tab, accept the thread Jonas resolved, apply Amara's suggestion "
        "or start the review Jonas asked you for.\n"
        "- On the References tab, see what the check found in refs.bib and take the record's values.\n"
        "- On the Versions tab, compare Submission with arXiv.\n\n"
        "Reminders for this paper are muted. When you are done, delete it on the Settings tab."
    ),
}

# (kind, days from the sign-in, time zone). Each is a day without a time.
DATES = (
    ("abstract", -38, "AoE"),
    ("submission", -31, "AoE"),
    ("rebuttal_start", -8, "AoE"),
    ("rebuttal_end", -2, "AoE"),
    ("notification", 9, "AoE"),
    ("camera_ready", 30, "AoE"),
    ("conference_start", 75, "UTC"),
    ("conference_end", 77, "UTC"),
)
# The day the submission deadline had before it was extended.
SUBMISSION_BEFORE = -35

# The sentence about the extension, written once the deadline had moved.
EXTENSION = (
    " The submission\ndeadline was extended by four days, and the overview still shows where it was\nbefore.",
    "",
)
# The submission is anonymous, the preprint is not.
AUTHORS = ("\\author{Anonymous Authors}", "\\author{Jonas Lindqvist \\and You \\and Amara Okafor}")
# Jonas added the issue number after Amara had checked the entry.
NUMBER = ("  volume  = {12},\n", "  volume  = {12},\n  number  = {1},\n")

# Amara's review of the submission: what she quoted, what she wrote, and the
# text Jonas replaced the quote with where he did.
AOE = "They are in AoE, as most calls for papers state them."
AOE_FIX = (
    "They are in AoE, Anywhere on Earth, as most calls for papers state them.\n"
    "An AoE deadline ends when its day is over everywhere, at noon of the next\n"
    "day in UTC."
)
PULL = "After the submission, Amara reviewed it like a pull request."
PULL_FIX = (
    "After the submission, Amara reviewed it. A review is a reading of one fixed\n"
    "state of the text that ends with a verdict."
)
VERSIONS = (
    "The Versions tab compiles a\nPDF for each of them and compares any two, as a word diff or as a PDF that\n"
    "marks every change."
)
VERSIONS_SUGGESTION = (
    "The Versions tab compiles a\nPDF of each one and compares any two states, as a word diff or as a latexdiff\n"
    "PDF that marks every change."
)
CAPTION = "The mistakes in this paper's bibliography."
SUMMARY = (
    "A clear tour of a paper's life. I have four small requests. The one about the time zone "
    "matters most, because people miss AoE deadlines every year."
)


def create_for(db: DBSession, user: User) -> Project | None:
    """Make the example paper for ``user`` and return it. A failure is logged
    and leaves nothing behind, because it must never stop a sign-in."""
    try:
        return _create(db, user)
    except Exception:
        logger.exception("The example paper for %s could not be made", user.email)
        db.rollback()
        return None


def _create(db: DBSession, user: User) -> Project:
    today = utcnow().date()
    jonas, amara = _people(db)
    project, dates = _project(db, user, jonas, amara, today)
    try:
        _fill(db, project, user, jonas, amara, dates, today)
        db.commit()
    except BaseException:
        # As a failed new project does (routers/projects.py), nothing stays.
        db.rollback()
        db.query(Project).filter(Project.id == project.id).delete()
        db.commit()
        live.delete_project(project.id)
        raise
    if get_settings().references_network:
        try:
            references.start(db, project, jonas, want_claims=False, refresh=False)
        except Exception:
            # The paper is there. Its References tab can start the check.
            logger.exception("The reference check of the example paper %s did not start", project.id)
    return project


def _people(db: DBSession) -> tuple[User, User]:
    """Jonas and Amara, made by the first example. Two first sign-ins at
    the same moment may both make them, and the second one reads them."""
    try:
        return _make_people(db)
    except IntegrityError:
        db.rollback()
        return _make_people(db)


def _make_people(db: DBSession) -> tuple[User, User]:
    people = []
    for email, name in (JONAS, AMARA):
        person = get_or_create_user(db, email)
        person.name = name
        person.reminders = False
        person.mention_mails = False
        people.append(person)
    db.commit()
    return people[0], people[1]


def _at(today: date, days: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(today + timedelta(days=days), time(hour, minute))


def _project(
    db: DBSession, owner: User, jonas: User, amara: User, today: date
) -> tuple[Project, dict[str, ProjectDate]]:
    """The project's rows, committed before its files are made (the live
    document's rows point at the project)."""
    project = Project(id=services.new_id(), owner_id=owner.id, labels=[], example=True)
    services.apply_fields(project, {**FIELDS, "year": (today + timedelta(days=DATES[-1][1])).year})
    project.name = naming.unique_name(db, owner.id, NAME)
    project.created_at = _at(today, -50, 9)
    db.add(project)
    db.flush()
    # Reminders about a made-up paper would only confuse. The paper's page
    # shows the mute, which can be lifted.
    for person, role, added in ((owner, "owner", None), (jonas, "editor", -46), (amara, "reviewer", -40)):
        membership = Membership(project_id=project.id, user_id=person.id, role=role, added_by=owner.id,
                                reminders_muted=True)
        if role == "owner":
            membership.position = tree.next_project_position(db, owner.id, None)
        else:
            membership.added_at = _at(today, added, 10)
        db.add(membership)

    dates: dict[str, ProjectDate] = {}
    for i, (kind, days, tz) in enumerate(DATES):
        day = (today + timedelta(days=days)).isoformat()
        d = services.new_date(project.id, DateIn(kind=kind, date=day, tz=tz), float(i + 1))
        d.created_at = project.created_at
        db.add(d)
        dates[kind] = d
    before, _ = when.parse_due(None, (today + timedelta(days=SUBMISSION_BEFORE)).isoformat(), "AoE")
    submission = dates["submission"]
    submission.changes = [{"at": when.iso(_at(today, -37, 8, 30)), "by": jonas.email,
                           "from": when.iso(before), "to": when.iso(submission.due_at)}]
    for kind in ("abstract", "submission"):
        d = dates[kind]
        d.answer = "yes"
        d.answered_at = d.due_at + timedelta(hours=2)
        d.answered_by = jonas.id
    db.commit()
    return project, dates


def _swap(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"The example expects {old!r} once, not {text.count(old)} times")
    return text.replace(old, new)


def _cut(text: str, start: str, end: str) -> str:
    """``text`` without what lies from ``start`` up to ``end``."""
    i = text.index(start)
    return text[:i] + text[text.index(end, i):]


def _entry(text: str, key: str) -> bibtex.Entry:
    return next(e for e in bibtex.parse(text, BIB).entries if e.key == key)


def _marked(text: str, marks: list[tuple[str, User, date]]) -> str:
    """``text`` with a meertex-checked mark in each entry, as "Mark as
    checked" writes it (DESIGN §16, The checked mark)."""
    for key, person, day in marks:
        entry = _entry(text, key)
        value = bibtex.mark_value(day, person.name or "", bibtex.fingerprint(entry))
        text = bibtex.set_field(text, entry, bibtex.MARK_FIELD, value)
    return text


def _stage(project_id: str, at: datetime, *edits: tuple[str, str, str]) -> str:
    """Write each ``(path, editor, text)`` into the live document and take
    the snapshot, dated ``at``. Returns its commit."""
    for path, editor, text in edits:
        live.write_file(project_id, path, text.encode(), editor)
    return live.snapshot(project_id, at=at)


def _fill(
    db: DBSession,
    project: Project,
    owner: User,
    jonas: User,
    amara: User,
    dates: dict[str, ProjectDate],
    today: date,
) -> None:
    """The files with their history, the versions, the review and the request."""
    pid = project.id

    def at(days: int, hour: int, minute: int = 0) -> datetime:
        return _at(today, days, hour, minute)

    submitted = (HERE / MAIN).read_text().replace(NAME, project.name)
    draft = _swap(submitted, *EXTENSION)
    first = _cut(draft, "\\section{Reviews}", "\\section{What Next}")
    preprint = _swap(submitted, *AUTHORS)
    revised = _swap(_swap(preprint, AOE, AOE_FIX), PULL, PULL_FIX)

    bib = (HERE / BIB).read_text()
    unchecked = _swap(bib, NUMBER[1], NUMBER[0])
    collabtex = _entry(unchecked, "collabtex")
    bib_first = (unchecked[: collabtex.start] + unchecked[collabtex.end :].lstrip()).rstrip() + "\n"
    marked_on = today - timedelta(days=38)
    checked = _marked(unchecked, [("shapiro2011crdt", jonas, marked_on), ("wilson2014best", amara, marked_on)])
    bib_submitted = _swap(checked, *NUMBER)

    live.init_project(pid, at=at(-50, 9))
    _stage(pid, at(-45, 16, 20), (MAIN, jonas.email, first), (BIB, jonas.email, bib_first))
    _stage(pid, at(-38, 11, 5), (MAIN, amara.email, draft), (BIB, jonas.email, checked))
    sent = _stage(pid, at(-31, 18, 40), (MAIN, jonas.email, submitted), (BIB, jonas.email, bib_submitted))
    posted = _stage(pid, at(-24, 10, 15), (MAIN, jonas.email, preprint))
    now = _stage(pid, at(-5, 15, 30), (MAIN, jonas.email, revised))

    submission = services.create_version(db, project, jonas, "submission", date=dates["submission"], at=sent,
                                         note="Handed in the evening before the deadline.")
    submission.created_at = dates["submission"].answered_at
    arxiv = services.create_version(db, project, jonas, "arxiv", at=posted, note="The preprint, with our names.")
    arxiv.created_at = at(-24, 10, 30)

    file_id = next(f["id"] for f in live.list_files(pid) if f["path"] == MAIN)
    base = live.read_file(pid, MAIN, sent).data.decode()
    review = Review(id=services.new_id(), project_id=pid, reviewer_id=amara.id, commit=sent,
                    version_id=submission.id, state="submitted", verdict="changes_requested", summary=SUMMARY,
                    mentions=mentions.mentioned_ids(db, project, SUMMARY), created_at=at(-21, 9),
                    submitted_at=at(-20, 17, 45))
    db.add(review)

    def thread(on: Review | None, commit: str, text: str, quote: str, made: datetime, body: str,
               suggestion: str | None = None) -> Thread:
        start = text.index(quote)
        line_start, line_end = lines_of(text, start, start + len(quote))
        t = Thread(id=services.new_id(), project_id=pid, review_id=on.id if on else None, created_by=amara.id,
                   file_id=file_id, base_commit=commit, base_path=MAIN, base_start=start,
                   base_end=start + len(quote), base_line_start=line_start, base_line_end=line_end, quote=quote,
                   state="open", created_at=made, updated_at=made)
        db.add(t)
        db.flush()
        comment(t, amara, made, body, suggestion=suggestion)
        return t

    def comment(t: Thread, person: User, made: datetime, body: str, kind: str = "comment",
                suggestion: str | None = None) -> None:
        db.add(Comment(id=services.new_id(), thread_id=t.id, author_id=person.id, kind=kind, body=body,
                       suggestion=suggestion, mentions=mentions.mentioned_ids(db, project, body), created_at=made))
        t.updated_at = made

    aoe = thread(review, sent, base, AOE, at(-21, 9, 10),
                 "Say what AoE means. Every year somebody misses a deadline because they read AoE as their own "
                 "midnight.")
    pull = thread(review, sent, base, PULL, at(-21, 9, 25),
                  "Not everybody knows pull requests. Can you say what a review is without GitHub?")
    thread(review, sent, base, VERSIONS, at(-21, 9, 40),
           "Name latexdiff here. Many readers know the tool by that name.", suggestion=VERSIONS_SUGGESTION)
    caption = thread(review, sent, base, CAPTION, at(-20, 17, 30),
                     "Should we fix these entries before the camera-ready?")
    comment(caption, jonas, at(-18, 8, 50),
            "No, they stay. They show what the References tab finds. Fix one and watch its row change.")

    # Jonas resolves with the text as it reads now, as the resolve route does.
    anchorer = Anchorer()
    for t, made, note in ((aoe, at(-5, 15, 35), "Spelled AoE out, with the time in UTC."),
                          (pull, at(-5, 15, 36), "Explained it without GitHub.")):
        span = anchorer.locate(t)
        t.state = "resolved"
        t.resolved_by = jonas.id
        t.resolved_at = made
        t.resolution_commit = now
        t.resolution_note = note
        t.resolution_diff = word_diff(t.quote, span.text, t.base_line_start, span.line_start)
        comment(t, jonas, made, note, kind="resolved")
    aoe.state = "accepted"
    aoe.accepted_by = amara.id
    aoe.accepted_at = at(-3, 10)
    comment(aoe, amara, at(-3, 10), "Thanks, that is clear now.", kind="accepted")

    # A single comment outside the review, on the text as it reads now.
    checkout = f"meertex checkout {project.name}"
    thread(None, now, revised, checkout, at(-2, 14),
           f"@{JONAS[1]} should we say where the folder ends up? It is ~/papers/{project.name} unless you "
           "name another one.")

    db.add(ReviewRequest(id=services.new_id(), project_id=pid, reviewer_id=owner.id, requested_by=jonas.id,
                         commit=now, note="I made the changes from Amara's review. Could you read the paper once "
                         "more before the notification?", state="open", created_at=at(-1, 9)))
