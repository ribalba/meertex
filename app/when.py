"""Deadlines and their time zones (DESIGN §5).

Venues state deadlines in their own way: "AoE", "UTC+2", "GMT-5" or a city's
zone. We keep how they said it (``tz``) next to the instant (``due_at``, naive
UTC like every datetime inside), so the web app can show the deadline the way
the CFP printed it and still count down to the right second.

Fixed offsets are read the way CFPs mean them: "GMT+1" is one hour ahead of
UTC. That is the opposite of the IANA ``Etc/GMT+1`` zone, which is why they are
parsed here before ZoneInfo ever sees them.
"""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AOE = timezone(timedelta(hours=-12), "AoE")
DEFAULT_TZ = "AoE"

_OFFSET = re.compile(r"^(UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# A date without a time is due at the last second of its day.
END_OF_DAY = time(23, 59, 59)


def _parse(tz: str) -> tuple[str, tzinfo]:
    value = (tz or "").strip()
    if not value:
        raise ValueError("The time zone is empty")
    if value.lower() == "aoe":
        return "AoE", AOE
    if value.upper() in ("UTC", "GMT", "Z"):
        return "UTC", UTC
    m = _OFFSET.match(value)
    if m:
        hours, minutes = int(m.group(3)), int(m.group(4) or 0)
        if hours > 14 or minutes > 59:
            raise ValueError(f"{value} is not a valid UTC offset")
        sign = 1 if m.group(2) == "+" else -1
        offset = sign * timedelta(hours=hours, minutes=minutes)
        name = f"{m.group(1).upper()}{m.group(2)}{hours}" + (f":{minutes:02d}" if minutes else "")
        return name, timezone(offset, name)
    try:
        return value, ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(
            f"Unknown time zone {value}. Use AoE, UTC, an offset like UTC+2 or a zone like Europe/Berlin."
        ) from exc


def canonical_tz(tz: str) -> str:
    """How ``tz`` is stored: ``AoE``, ``UTC``, ``UTC+2``, ``GMT-5:30`` or the IANA
    name. Raises ValueError for a zone nobody knows."""
    return _parse(tz)[0]


def zone(tz: str) -> tzinfo:
    """The tzinfo for a stored ``tz``. Raises ValueError for an unknown one."""
    return _parse(tz)[1]


def _to_utc(aware: datetime) -> datetime:
    return aware.astimezone(UTC).replace(tzinfo=None)


def from_date(day: str, tz: str) -> datetime:
    """``YYYY-MM-DD`` in ``tz`` to the naive UTC instant of its last second."""
    try:
        d = date.fromisoformat(day.strip())
    except ValueError as exc:
        raise ValueError(f"{day} is not a date. Use YYYY-MM-DD.") from exc
    return _to_utc(datetime.combine(d, END_OF_DAY, tzinfo=zone(tz)))


def from_iso(value: str, tz: str) -> datetime:
    """An ISO 8601 time to naive UTC. Without an offset it is local to ``tz``."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{value} is not an ISO 8601 time, e.g. 2027-01-15T23:59 or 2027-01-15T12:00:00Z.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone(tz))
    return _to_utc(dt)


def parse_due(due_at: str | None, day: str | None, tz: str) -> tuple[datetime, bool]:
    """The instant and ``date_only`` from the API's ``due_at`` or ``date``. A
    ``due_at`` that is only a day counts as a ``date``. Raises ValueError."""
    if day:
        return from_date(day, tz), True
    if due_at:
        if _DAY.match(due_at.strip()):
            return from_date(due_at, tz), True
        return from_iso(due_at, tz), False
    raise ValueError("Give the date as due_at (ISO 8601) or as date (YYYY-MM-DD)")


def local(dt: datetime, tz: str) -> datetime:
    """A naive UTC instant as an aware datetime in ``tz``."""
    return dt.replace(tzinfo=UTC).astimezone(zone(tz))


def iso(dt: datetime | None) -> str | None:
    """Naive UTC to ISO 8601 with ``Z``, the API's format for every datetime."""
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def iso_local(dt: datetime, tz: str) -> str:
    """A naive UTC instant as ISO 8601 in ``tz``, with its offset."""
    return local(dt, tz).replace(microsecond=0).isoformat()


def day_text(d: date | datetime) -> str:
    """``15 Jan 2027``, the same in every locale."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def describe(dt: datetime, tz: str, date_only: bool) -> str:
    """How a date reads in a sentence, in the venue's zone: ``15 Jan 2027`` for
    a day, ``15 Jan 2027 at 14:00 UTC+2`` for a time."""
    when = local(dt, tz)
    if date_only:
        return day_text(when)
    return f"{day_text(when)} at {when:%H:%M} {canonical_tz(tz)}"


def move_local(dt: datetime, old_tz: str, new_tz: str, date_only: bool) -> datetime:
    """The same wall-clock deadline in another zone. Used when only ``tz`` of a
    date changes: the venue said "15 Jan, 14:00" and we had the zone wrong."""
    wall = local(dt, old_tz).replace(tzinfo=None)
    if date_only:
        wall = datetime.combine(wall.date(), END_OF_DAY)
    return _to_utc(wall.replace(tzinfo=zone(new_tz)))
