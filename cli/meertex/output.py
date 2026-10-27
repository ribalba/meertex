"""Human output: aligned tables, countdowns and colour.

Colour only goes to a terminal and never when NO_COLOR is set, so piped
output and ``--json`` stay plain. Countdowns follow the web app (§5): red
under 3 days, amber under 14.
"""

import json
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;]*m")

RED, AMBER, GREEN, DIM, BOLD, CYAN = "31", "33", "32", "2", "1", "36"

OUTCOMES = {
    "accepted": ("accepted", GREEN),
    "rejected": ("rejected", RED),
    "revision": ("revision", AMBER),
    "withdrawn": ("withdrawn", DIM),
    "not_submitted": ("not submitted", DIM),
}


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    return not os.environ.get("NO_COLOR") and hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, code: str | None, stream=None) -> str:
    if not code or not text or not use_color(stream):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def width(text: str) -> int:
    plain = ANSI.sub("", text)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain)


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def table(rows: list[list[str]], headers: list[str] | None = None, indent: str = "") -> str:
    """Columns padded to their widest cell, two spaces apart. The last column
    is not padded, so long text there does not push trailing spaces."""
    all_rows = ([headers] if headers else []) + rows
    if not all_rows:
        return ""
    cols = max(len(r) for r in all_rows)
    widths = [max((width(r[i]) for r in all_rows if i < len(r)), default=0) for i in range(cols)]
    lines = []
    for n, row in enumerate(all_rows):
        cells = []
        for i, cell in enumerate(row):
            pad = "" if i == len(row) - 1 else " " * (widths[i] - width(cell))
            cells.append(cell + pad)
        line = indent + "  ".join(cells).rstrip()
        if headers and n == 0:
            line = paint(line, DIM)
        lines.append(line)
    return "\n".join(lines)


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


# --- Time --------------------------------------------------------------------------


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def count(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def span(seconds: float) -> str:
    s = abs(int(seconds))
    if s < 60:
        return "less than a minute"
    if s < 3600:
        return count(s // 60, "minute")
    if s < 48 * 3600:
        return count(s // 3600, "hour")
    if s < 730 * 86400:
        return count(s // 86400, "day")
    return count(s // (365 * 86400), "year")


def countdown(seconds_left: float | None, stream=None) -> str:
    """"in 12 days", coloured by urgency, or "3 days ago" once passed."""
    if seconds_left is None:
        return ""
    if seconds_left < 0:
        return paint(f"{span(seconds_left)} ago", DIM, stream)
    text = f"in {span(seconds_left)}"
    if seconds_left < 3 * 86400:
        return paint(text, RED, stream)
    if seconds_left < 14 * 86400:
        return paint(text, AMBER, stream)
    return text


def ago(value: str | None) -> str:
    dt = parse_time(value)
    if dt is None:
        return "never"
    seconds = (datetime.now(UTC) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    return f"{span(seconds)} ago"


def when(d: dict | None) -> str:
    """A date as the venue states it, like ``2027-01-15 AoE`` or ``2027-01-15 23:59 Europe/Berlin``."""
    if not d:
        return ""
    local = d.get("local") or d.get("due_at") or ""
    tz = d.get("tz") or ""
    if d.get("date_only"):
        return f"{local[:10]} {tz}".strip()
    return f"{local[:16].replace('T', ' ')} {tz}".strip()


def day(value: str | None) -> str:
    dt = parse_time(value)
    return dt.strftime("%Y-%m-%d") if dt else ""


def outcome(value: str | None, stream=None) -> str:
    if not value:
        return ""
    label, code = OUTCOMES.get(value, (value, None))
    return paint(label, code, stream)


def next_date(nd: dict | None, stream=None) -> str:
    if not nd:
        return ""
    return f"{nd.get('label') or nd.get('kind')} {countdown(nd.get('seconds_left'), stream)}"


def venue(p: dict) -> str:
    return " ".join(str(x) for x in (p.get("conference"), p.get("year")) if x)
