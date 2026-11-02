"""The instance dashboard (DESIGN §18): the accounts, the papers and what
happens on the instance, for the admins INSTANCE_ADMINS names. For everybody
else the route does not exist (404), like a project that is not theirs.

The example papers (DESIGN §19) and their made-up coauthors are left out of
every number, because every new account gets one and nobody wrote it.
"""

import os
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import DateTime, cast, func, select, text

from .. import when
from ..config import get_settings
from ..database import DB
from ..example import PEOPLE
from ..live import git
from ..models import (
    ActiveDay,
    Comment,
    Compile,
    Membership,
    Project,
    Review,
    Thread,
    User,
    Version,
    utcnow,
)
from ..security import SessionOrTokenUser

router = APIRouter(prefix="/api/admin", tags=["admin"])

# A period and the bucket its charts count in: 30 days, 13 ISO weeks, 12 months.
PERIODS = {"30d": "day", "90d": "week", "12m": "month"}
# The newest accounts the list shows.
ACCOUNT_ROWS = 200


def _instance_admin(user: SessionOrTokenUser) -> User:
    if not user.instance_admin:
        raise HTTPException(status_code=404, detail="Not found")
    return user


InstanceAdmin = Annotated[User, Depends(_instance_admin)]


def _bucket_of(day: date, unit: str) -> date:
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    return day


def _buckets(unit: str, today: date) -> list[date]:
    """Each bucket's first day, oldest first, the last one holding today."""
    if unit == "day":
        return [today - timedelta(days=i) for i in range(29, -1, -1)]
    if unit == "week":
        monday = _bucket_of(today, "week")
        return [monday - timedelta(weeks=i) for i in range(12, -1, -1)]
    months = today.year * 12 + today.month - 1
    return [date((months - i) // 12, (months - i) % 12 + 1, 1) for i in range(11, -1, -1)]


def _counts(db: DB, column, unit: str, since: datetime, *filters, distinct=None) -> dict[date, int]:
    """Rows per bucket, or distinct values of ``distinct`` per bucket."""
    bucket = func.date_trunc(unit, cast(column, DateTime)).label("bucket")
    count = func.count(func.distinct(distinct)) if distinct is not None else func.count()
    rows = db.query(bucket, count).filter(cast(column, DateTime) >= since, *filters).group_by(bucket).all()
    return {b.date(): n for b, n in rows}


def _series(counts: dict[date, int], buckets: list[date]) -> list[int]:
    return [counts.get(b, 0) for b in buckets]


def _examples():
    """The ids of the example papers, for leaving their rows out."""
    return select(Project.id).where(Project.example.is_(True))


def _snapshots(db: DB, unit: str, since: datetime) -> dict[date, int]:
    """The commits the live text got (§9), from every project's history."""
    counts: dict[date, int] = {}
    for (project_id,) in db.query(Project.id).filter(Project.example.is_(False)):
        for at in git.commit_times(project_id, since):
            b = _bucket_of(at.date(), unit)
            counts[b] = counts.get(b, 0) + 1
    return counts


def _files_bytes() -> int:
    """Everything under DATA_DIR: the histories, the blobs, the avatars."""
    total = 0
    for root, _dirs, names in os.walk(get_settings().data_dir):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _accounts(db: DB) -> list[dict]:
    papers = dict(
        db.query(Membership.user_id, func.count())
        .filter(Membership.project_id.not_in(_examples()))
        .group_by(Membership.user_id)
        .all()
    )
    last = dict(db.query(ActiveDay.user_id, func.max(ActiveDay.day)).group_by(ActiveDay.user_id).all())
    # Whoever added the account to a paper first. An owner added themselves.
    inviter = (
        db.query(Membership.user_id, User)
        .join(User, User.id == Membership.added_by)
        .filter(Membership.role != "owner")
        .order_by(Membership.user_id, Membership.added_at)
        .distinct(Membership.user_id)
        .all()
    )
    invited_by = dict(inviter)
    users = (
        db.query(User)
        .filter(User.email.not_in(PEOPLE))
        .order_by(User.first_sign_in_at.desc().nulls_last(), User.created_at.desc())
        .limit(ACCOUNT_ROWS)
        .all()
    )
    return [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "avatar_url": u.avatar_url,
            "created_at": when.iso(u.created_at),
            "first_sign_in_at": when.iso(u.first_sign_in_at),
            "last_active": last[u.id].isoformat() if u.id in last else None,
            "papers": papers.get(u.id, 0),
            "invited_by": (
                {"email": invited_by[u.id].email, "name": invited_by[u.id].name} if u.id in invited_by else None
            ),
        }
        for u in users
    ]


@router.get("/instance")
def instance(db: DB, admin: InstanceAdmin, period: Literal["30d", "90d", "12m"] = "30d") -> dict:
    """The dashboard in one answer. ``totals`` and ``storage`` are the
    instance now. ``series`` counts per bucket of ``period``, and ``growth``
    is how many signed-in accounts and papers there were at each bucket's end."""
    unit = PERIODS[period]
    now = utcnow()
    today = now.date()
    buckets = _buckets(unit, today)
    since = datetime.combine(buckets[0], time())

    signed_in = db.query(func.count(User.id)).filter(User.first_sign_in_at.is_not(None)).scalar()
    accounts_total = db.query(func.count(User.id)).filter(User.email.not_in(PEOPLE)).scalar()
    real = Project.example.is_(False)
    papers_total = db.query(func.count(Project.id)).filter(real).scalar()

    def active_since(days: int) -> int:
        return (
            db.query(func.count(func.distinct(ActiveDay.user_id)))
            .filter(ActiveDay.day > today - timedelta(days=days))
            .scalar()
        )

    new_accounts = _counts(db, User.first_sign_in_at, unit, since)
    new_papers = _counts(db, Project.created_at, unit, since, real)
    example_threads = select(Thread.id).where(Thread.project_id.in_(_examples()))
    compiles = _counts(db, Compile.created_at, unit, since, Compile.status != "unavailable",
                       Compile.project_id.not_in(_examples()))
    comments = _counts(db, Comment.created_at, unit, since, Comment.kind == "comment",
                       Comment.thread_id.not_in(example_threads))
    reviews = _counts(db, Review.submitted_at, unit, since, Review.state == "submitted",
                      Review.project_id.not_in(_examples()))
    series = {
        "active": _series(_counts(db, ActiveDay.day, unit, since, distinct=ActiveDay.user_id), buckets),
        "accounts": _series(new_accounts, buckets),
        "papers": _series(new_papers, buckets),
        "snapshots": _series(_snapshots(db, unit, since), buckets),
        "compiles": _series(compiles, buckets),
        "comments": _series(comments, buckets),
        "reviews": _series(reviews, buckets),
        "versions": _series(_counts(db, Version.created_at, unit, since, Version.project_id.not_in(_examples())),
                            buckets),
    }

    def growth(before: int, added: list[int]) -> list[int]:
        out = []
        for n in added:
            before += n
            out.append(before)
        return out

    return {
        "base_url": get_settings().base_url,
        "generated_at": when.iso(now),
        "period": period,
        "unit": unit,
        "buckets": [b.isoformat() for b in buckets],
        "totals": {
            "accounts": accounts_total,
            "signed_in": signed_in,
            "waiting": accounts_total - signed_in,
            "papers": papers_total,
            "accepted": db.query(func.count(Project.id)).filter(real, Project.outcome == "accepted").scalar(),
            "planned": db.query(func.count(Project.id)).filter(real, Project.planned.is_(True)).scalar(),
            "active_7d": active_since(7),
            "active_30d": active_since(30),
        },
        "storage": {
            "database_bytes": db.execute(text("SELECT pg_database_size(current_database())")).scalar(),
            "files_bytes": _files_bytes(),
        },
        "series": series,
        "growth": {
            "accounts": growth(signed_in - sum(series["accounts"]), series["accounts"]),
            "papers": growth(papers_total - sum(series["papers"]), series["papers"]),
        },
        "accounts": _accounts(db),
        "accounts_total": accounts_total,
    }
