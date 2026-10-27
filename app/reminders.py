"""The daily reminder mail before deadlines (DESIGN §5, Reminders).

One mail a day per person, never one per date. It covers the papers where the
person is the owner or an editor, leaves out archived and muted ones, and
lists per paper:

* the dates something is handed in for, once they come within one of the
  person's reminder days (14, 3 and 1 days before and the day itself by
  default), under the same rules as the next date (§5),
* "start writing" when the writing window opens within the next day,
* the questions (§6) whose date passed since the last mail.

Days are counted in the venue's calendar. A deadline on 15 Jan AoE is "today"
for as long as it is 15 Jan somewhere on earth.

Every reminder told is a row in ``reminders_sent``, one per (person, date,
kind, threshold, due_at). Nothing is told twice, also not after a restart, and
a date that moved reminds again because its ``due_at`` is a new one.

The loop runs in the app's lifespan and works in a thread. It wakes every few
minutes and sends the mail of everyone whose own hour has come and who had
none yet on their own today. Each person gets a transaction of their own with
their row locked (a locked row is skipped), so one failing mail never stops
the others and a second copy of the app never sends the same mail.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo

import anyio
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DBSession

from . import emailer, lifecycle, serialize, services, when
from .config import get_settings
from .database import SessionLocal
from .models import (
    DEFAULT_REMINDER_DAYS,
    Membership,
    Project,
    ProjectDate,
    ReminderSent,
    User,
    utcnow,
)

logger = logging.getLogger("uvicorn.error")

# Dates something is handed in for. The others (rebuttal opens, notification,
# the conference) need nothing from the authors by then.
HAND_IN = frozenset({"abstract", "submission", "supplementary", "rebuttal_end", "revision", "camera_ready", "other"})
# Who gets reminders. Reviewers and viewers have nothing to hand in.
ROLES = ("owner", "editor")
# How often the loop looks for mails that are due.
TICK_SECONDS = 300
# "Start writing" shows up when the window opens within a day of the mail.
WRITING_AHEAD = timedelta(days=1)
# Rows about dates this long gone can never match again and are deleted.
KEEP = timedelta(days=90)
MAX_DAY = 365
MAX_THRESHOLDS = 10

LogKey = tuple[str, str, int, datetime]


# --- Days and time ----------------------------------------------------------------


def clean_days(values: list) -> list[int]:
    """Reminder days as they are stored. Whole days from 0 (the day itself) to
    365, each once, the largest first. Raises ValueError for anything else."""
    if len(values) > MAX_THRESHOLDS:
        raise ValueError(f"At most {MAX_THRESHOLDS} reminder days")
    out: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_DAY:
            raise ValueError(f"Reminder days are whole days from 0 (the day itself) to {MAX_DAY}")
        out.add(value)
    return sorted(out, reverse=True)


def thresholds(user: User) -> list[int]:
    """The user's reminder days. Stored values that make no sense are skipped."""
    stored = user.reminder_days if user.reminder_days is not None else list(DEFAULT_REMINDER_DAYS)
    good = {v for v in stored if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_DAY}
    return sorted(good, reverse=True)


def days_left(d: ProjectDate, now: datetime) -> int:
    """Whole days from today to the date's day, both in the venue's zone."""
    return (when.local(d.due_at, d.tz).date() - when.local(now, d.tz).date()).days


def countdown(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def when_text(d: ProjectDate) -> str:
    """The date as the venue states it. ``15 Jan 2027 (AoE)`` for a day,
    ``15 Jan 2027 at 14:00 UTC+2`` for a time."""
    text = when.describe(d.due_at, d.tz, d.date_only)
    return f"{text} ({when.canonical_tz(d.tz)})" if d.date_only else text


def user_zone(user: User) -> tzinfo:
    try:
        return when.zone(user.timezone or "UTC")
    except ValueError:
        return UTC


def _here(user: User, instant: datetime) -> datetime:
    """A naive UTC instant in the user's own zone."""
    return instant.replace(tzinfo=UTC).astimezone(user_zone(user))


def _done_today(user: User, now: datetime) -> bool:
    return user.reminded_at is not None and _here(user, user.reminded_at).date() >= _here(user, now).date()


def is_due(user: User, now: datetime) -> bool:
    """Whether the user's mail goes out now. That is when reminders are on,
    their hour has come in their own zone, and today (theirs) had no mail yet."""
    if not user.reminders:
        return False
    return _here(user, now).hour >= user.reminder_hour and not _done_today(user, now)


def next_at(user: User, now: datetime) -> datetime | None:
    """When the next mail is put together (naive UTC), or None while reminders
    are off. A mail that is due already goes out with the loop's next round."""
    if not user.reminders:
        return None
    here = _here(user, now)
    day = here.date() + timedelta(days=1) if _done_today(user, now) else here.date()
    at = datetime.combine(day, time(user.reminder_hour), tzinfo=user_zone(user))
    return max(at.astimezone(UTC).replace(tzinfo=None), now)


# --- What a mail says -------------------------------------------------------------


@dataclass
class Digest:
    """The papers of one mail with their items, and the rows that record them."""

    papers: list[dict] = field(default_factory=list)
    keys: set[LogKey] = field(default_factory=set)
    muted: list[dict] = field(default_factory=list)


def _label(d: ProjectDate) -> str:
    return d.label or lifecycle.DATE_LABELS.get(d.kind) or "Other"


def _date_fields(d: ProjectDate) -> dict:
    return {
        "date_id": d.id,
        "kind": d.kind,
        "label": _label(d),
        "due_at": when.iso(d.due_at),
        "tz": d.tz,
        "date_only": d.date_only,
        "local": when.iso_local(d.due_at, d.tz),
        "when": when_text(d),
    }


def _date_items(project: Project, dates: list[ProjectDate], days: list[int], sent: set[LogKey] | None,
                now: datetime) -> list[tuple[datetime, dict, list[LogKey]]]:
    """A date reminds when the tightest of the user's days it is within was not
    told yet. The wider ones are recorded with it, so adding a day later on
    does not tell the same thing again."""
    out = []
    for d in lifecycle.dates_ahead(project, dates, now):
        if d.kind not in HAND_IN:
            continue
        left = days_left(d, now)
        crossed = [t for t in days if left <= t]
        if not crossed:
            continue
        tightest = min(crossed)
        if sent is not None and (d.id, "date", tightest, d.due_at) in sent:
            continue
        item = {
            "type": "date",
            **_date_fields(d),
            "days_left": left,
            "threshold": tightest,
            "text": f"{_label(d)} {countdown(left)}, {when_text(d)}.",
        }
        out.append((d.due_at, item, [(d.id, "date", t, d.due_at) for t in crossed]))
    return out


def _writing_item(user: User, project: Project, dates: list[ProjectDate], submitted: bool,
                  sent: set[LogKey] | None, now: datetime) -> tuple[datetime, dict, list[LogKey]] | None:
    """The "start writing" item, when the writing window (§5) opens within a
    day of now. The day before counts too, for a mail that could not go out
    yesterday."""
    window = lifecycle.writing_window(project, dates, submitted, user.writing_days)
    if window is None:
        return None
    writing_days, target, start = window
    if target.due_at < now or not now - WRITING_AHEAD <= start < now + WRITING_AHEAD:
        return None
    key = (target.id, "writing", writing_days, start)
    if sent is not None and key in sent:
        return None
    starts = "tomorrow" if (_here(user, start).date() - _here(user, now).date()).days >= 1 else "today"
    left = days_left(target, now)
    item = {
        "type": "writing",
        **_date_fields(target),
        "start": when.iso(start),
        "writing_days": writing_days,
        "days_left": left,
        "text": f"Start writing {starts}. {_label(target)} is {countdown(left)}, {when_text(target)}, "
                f"and writing takes {writing_days} days.",
    }
    return start, item, [key]


def _question_items(project: Project, dates: list[ProjectDate], submitted: bool, sent: set[LogKey] | None,
                    since: datetime | None, now: datetime) -> list[tuple[datetime, dict, list[LogKey]]]:
    """Questions (§6) whose date passed after ``since``, the last mail."""
    out = []
    for d in dates:
        if not lifecycle.asks(project, d, submitted, now):
            continue
        key = (d.id, "question", 0, d.due_at)
        if sent is not None and (key in sent or (since is not None and d.due_at <= since)):
            continue
        item = {
            "type": "question",
            **_date_fields(d),
            "text": lifecycle.question_text(d),
            "answers": list(lifecycle.answers_for(d.kind)),
        }
        out.append((d.due_at, item, [key]))
    return out


def _paper(project: Project, membership: Membership, items: list[dict]) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "title": project.title,
        "conference": project.conference,
        "year": project.year,
        "planned": project.planned,
        "role": membership.role,
        "url": serialize.project_url(project.name),
        "items": items,
    }


def build(db: DBSession, user: User, now: datetime, *, fresh_only: bool) -> Digest:
    """What a mail to ``user`` says at ``now``. With ``fresh_only`` (the mail)
    it leaves out what ``reminders_sent`` says was told, and questions older
    than the last mail. Without it (the preview) everything due counts."""
    rows = (
        db.query(Project, Membership)
        .join(Membership, Membership.project_id == Project.id)
        .filter(Membership.user_id == user.id, Membership.role.in_(ROLES), Project.archived.is_(False))
        .order_by(Project.name)
        .all()
    )
    digest = Digest(muted=[{"id": p.id, "name": p.name} for p, m in rows if m.reminders_muted])
    rows = [(p, m) for p, m in rows if not m.reminders_muted]
    ids = [p.id for p, _ in rows]
    dates = services.dates_by_project(db, ids)
    submitted = services.submitted_projects(db, ids)
    sent: set[LogKey] | None = None
    since: datetime | None = None
    if fresh_only:
        sent = {
            tuple(row)
            for row in db.query(ReminderSent.date_id, ReminderSent.kind, ReminderSent.threshold, ReminderSent.due_at)
            .filter(ReminderSent.user_id == user.id)
            .all()
        }
        since = user.reminded_at or now - timedelta(days=1)
    days = thresholds(user)

    papers = []
    for project, membership in rows:
        ds, handed_in = dates[project.id], project.id in submitted
        found = _date_items(project, ds, days, sent, now)
        writing = _writing_item(user, project, ds, handed_in, sent, now)
        if writing:
            # "Start writing" already names the deadline and its countdown, so
            # a reminder of the same date in the same mail would say it twice.
            # Its keys are still recorded, so it does not come back tomorrow.
            same = [f for f in found if f[1]["date_id"] == writing[1]["date_id"]]
            for f in same:
                found.remove(f)
                writing = (writing[0], writing[1], writing[2] + f[2])
            found.append(writing)
        found.extend(_question_items(project, ds, handed_in, sent, since, now))
        if not found:
            continue
        order = {"question": 0, "writing": 1, "date": 2}
        found.sort(key=lambda f: (f[0], order[f[1]["type"]]))
        for _at, _item, keys in found:
            digest.keys.update(keys)
        papers.append((found[0][0], _paper(project, membership, [item for _at, item, _keys in found])))
    papers.sort(key=lambda p: (p[0], p[1]["name"]))
    digest.papers = [paper for _first, paper in papers]
    return digest


# --- The mail -------------------------------------------------------------------


def _base() -> str:
    return get_settings().base_url.rstrip("/")


def _head(paper: dict) -> str:
    venue = " ".join(str(x) for x in (paper["conference"], paper["year"]) if x)
    if paper["planned"]:
        return f"{venue or paper['name']}, still a plan"
    title = (paper["title"] or "").strip()
    if title and venue:
        return f"{title} ({venue})"
    return title or venue or paper["name"]


def _subject(papers: list[dict]) -> str:
    """The most pressing item, and how many more there are."""
    entries = [(p, i) for p in papers for i in p["items"]]
    rank = {"date": 0, "writing": 1, "question": 2}
    paper, item = min(entries, key=lambda e: (rank[e[1]["type"]], e[1].get("days_left", 0), e[1]["due_at"]))
    if item["type"] == "date":
        head = f"{item['label']} {countdown(item['days_left'])} for {paper['name']}"
    elif item["type"] == "writing":
        head = f"Start writing {paper['name']}"
    else:
        head = f"A question about {paper['name']}"
    more = len(entries) - 1
    return f"{head} and {more} more" if more else head


def render(user: User, papers: list[dict]) -> tuple[str, str] | None:
    """(subject, body) of the mail, or None when there is nothing to say."""
    if not papers:
        return None
    lines = ["Hi,", "", "Here is what is coming up in your papers.", ""]
    for paper in papers:
        lines.append(_head(paper))
        lines.extend(f"- {item['text']}" for item in paper["items"])
        lines.append(paper["url"])
        lines.append("")
    lines.append(f"Every paper and its writing time is on your timeline at {_base()}/timeline.")
    lines.append("")
    lines.append(
        f"This mail comes at most once a day, at {user.reminder_hour:02d}:00 {user.timezone or 'UTC'}, "
        f"when there is something to say. Change when or turn it off at {_base()}/settings#reminders. "
        "A paper's own page can mute it."
    )
    return _subject(papers), "\n".join(lines)


def preview(db: DBSession, user: User, now: datetime) -> dict:
    """What GET /api/reminders/preview returns. The next mail, leaving out
    the record of what was told and the hour. It shows every date within the
    user's reminder days and every waiting question, not only the new ones."""
    digest = build(db, user, now, fresh_only=False)
    mail = render(user, digest.papers)
    return {
        "reminders": user.reminders,
        "reminder_days": thresholds(user),
        "reminder_hour": user.reminder_hour,
        "timezone": user.timezone,
        "sending": get_settings().reminders_enabled,
        "last_at": when.iso(user.reminded_at),
        "next_at": when.iso(next_at(user, now)),
        "projects": digest.papers,
        "muted": digest.muted,
        "subject": mail[0] if mail else None,
        "body": mail[1] if mail else None,
        "timeline_url": f"{_base()}/timeline",
        "settings_url": f"{_base()}/settings#reminders",
    }


# --- Sending --------------------------------------------------------------------


def _record(db: DBSession, user_id: str, keys: set[LogKey], now: datetime) -> None:
    if not keys:
        return
    rows = [
        {"user_id": user_id, "date_id": date_id, "kind": kind, "threshold": threshold, "due_at": due_at, "sent_at": now}
        for date_id, kind, threshold, due_at in sorted(keys)
    ]
    db.execute(pg_insert(ReminderSent).values(rows).on_conflict_do_nothing(constraint="uq_reminders_sent"))


def send_one(user_id: str, now: datetime) -> bool:
    """Put together and send one person's mail if it is due. Returns whether a
    mail went out. The record and the mail commit together: a mail that fails
    leaves nothing behind and is tried again on the next round."""
    with SessionLocal() as db:
        user = (
            db.query(User)
            .filter(User.id == user_id)
            # NO KEY UPDATE, so rows that only point at the user (sessions, comments) can still be written.
            .with_for_update(key_share=True, skip_locked=True)
            .one_or_none()
        )
        if user is None or not is_due(user, now):
            return False
        digest = build(db, user, now, fresh_only=True)
        _record(db, user.id, digest.keys, now)
        user.reminded_at = now
        db.flush()
        mail = render(user, digest.papers)
        if mail is not None:
            emailer.send_email(user.email, *mail)
        db.commit()
        return mail is not None


def send_due(now: datetime | None = None) -> int:
    """Send every mail that is due at ``now``. Returns how many went out."""
    now = now or utcnow()
    with SessionLocal() as db:
        db.query(ReminderSent).filter(ReminderSent.due_at < now - KEEP).delete(synchronize_session=False)
        db.commit()
        due = [u.id for u in db.query(User).filter(User.reminders.is_(True)).all() if is_due(u, now)]
    sent = 0
    for user_id in due:
        try:
            sent += send_one(user_id, now)
        except Exception:
            logger.exception("The reminder mail for user %s failed", user_id)
    return sent


async def _loop() -> None:
    while True:
        try:
            await anyio.to_thread.run_sync(send_due)
        except Exception:
            logger.exception("Sending reminder mails failed")
        await anyio.sleep(TICK_SECONDS)


@asynccontextmanager
async def running():
    """The reminder loop for the app's lifetime, unless REMINDERS_ENABLED is off."""
    if not get_settings().reminders_enabled:
        yield
        return
    async with anyio.create_task_group() as tg:
        tg.start_soon(_loop)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()
